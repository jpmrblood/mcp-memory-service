# Copyright 2024 Heinrich Krupp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Hybrid memory storage backend for MCP Memory Service.

This implementation provides the best of both worlds:
- SQLite-vec as primary storage for ultra-fast reads (~5ms)
- Cloudflare as secondary storage for cloud persistence and multi-device sync
- Background synchronization service for seamless integration
- Graceful degradation when cloud services are unavailable
"""

import asyncio
import logging
import time
from typing import List, Dict, Any, Tuple, Optional
from collections import deque
from dataclasses import dataclass

from .base import MemoryStorage
from .sqlite_vec import SqliteVecMemoryStorage
from .cloudflare import CloudflareStorage
from ..models.memory import Memory, MemoryQueryResult

# Import config to check if limit constants are available
try:
    from ..config import (
        CLOUDFLARE_D1_MAX_SIZE_GB,
        CLOUDFLARE_VECTORIZE_MAX_VECTORS,
        CLOUDFLARE_MAX_METADATA_SIZE_KB,
        CLOUDFLARE_WARNING_THRESHOLD_PERCENT,
        CLOUDFLARE_CRITICAL_THRESHOLD_PERCENT
    )
except ImportError:
    # Fallback values if config doesn't have them
    CLOUDFLARE_D1_MAX_SIZE_GB = 10
    CLOUDFLARE_VECTORIZE_MAX_VECTORS = 5_000_000
    CLOUDFLARE_MAX_METADATA_SIZE_KB = 10
    CLOUDFLARE_WARNING_THRESHOLD_PERCENT = 80
    CLOUDFLARE_CRITICAL_THRESHOLD_PERCENT = 95

logger = logging.getLogger(__name__)

@dataclass
class SyncOperation:
    """Represents a pending sync operation."""
    operation: str  # 'store', 'delete', 'update'
    memory: Optional[Memory] = None
    content_hash: Optional[str] = None
    updates: Optional[Dict[str, Any]] = None
    timestamp: float = None
    retries: int = 0
    max_retries: int = 3

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()

class BackgroundSyncService:
    """
    Handles background synchronization between SQLite-vec and Cloudflare.

    Features:
    - Asynchronous operation queue
    - Retry logic with exponential backoff
    - Health monitoring and error handling
    - Configurable sync intervals and batch sizes
    - Graceful degradation when cloud is unavailable
    """

    def __init__(self,
                 primary_storage: SqliteVecMemoryStorage,
                 secondary_storage: CloudflareStorage,
                 sync_interval: int = 300,  # 5 minutes
                 batch_size: int = 50,
                 max_queue_size: int = 1000):
        self.primary = primary_storage
        self.secondary = secondary_storage
        self.sync_interval = sync_interval
        self.batch_size = batch_size
        self.max_queue_size = max_queue_size

        # Sync queues and state
        self.operation_queue = asyncio.Queue(maxsize=max_queue_size)
        self.failed_operations = deque(maxlen=100)  # Keep track of failed operations
        self.is_running = False
        self.sync_task = None
        self.last_sync_time = 0
        self.sync_stats = {
            'operations_processed': 0,
            'operations_failed': 0,
            'last_sync_duration': 0,
            'cloudflare_available': True
        }

        # Health monitoring
        self.consecutive_failures = 0
        self.max_consecutive_failures = 5
        self.backoff_time = 60  # Start with 1 minute backoff

        # Cloudflare capacity tracking
        self.cloudflare_stats = {
            'vector_count': 0,
            'estimated_d1_size_gb': 0,
            'last_capacity_check': 0,
            'approaching_limits': False,
            'limit_warnings': []
        }

    async def start(self):
        """Start the background sync service."""
        if self.is_running:
            logger.warning("Background sync service is already running")
            return

        self.is_running = True
        self.sync_task = asyncio.create_task(self._sync_loop())
        logger.info(f"Background sync service started with {self.sync_interval}s interval")

    async def stop(self):
        """Stop the background sync service and process remaining operations."""
        if not self.is_running:
            return

        self.is_running = False

        # Process remaining operations in queue
        remaining_operations = []
        while not self.operation_queue.empty():
            try:
                operation = self.operation_queue.get_nowait()
                remaining_operations.append(operation)
            except asyncio.QueueEmpty:
                break

        if remaining_operations:
            logger.info(f"Processing {len(remaining_operations)} remaining operations before shutdown")
            await self._process_operations_batch(remaining_operations)

        # Cancel the sync task
        if self.sync_task:
            self.sync_task.cancel()
            try:
                await self.sync_task
            except asyncio.CancelledError:
                pass

        logger.info("Background sync service stopped")

    async def enqueue_operation(self, operation: SyncOperation):
        """Enqueue a sync operation for background processing."""
        try:
            await self.operation_queue.put(operation)
            logger.debug(f"Enqueued {operation.operation} operation")
        except asyncio.QueueFull:
            # If queue is full, process immediately to avoid blocking
            logger.warning("Sync queue full, processing operation immediately")
            await self._process_single_operation(operation)

    async def force_sync(self) -> Dict[str, Any]:
        """Force an immediate full synchronization between backends."""
        logger.info("Starting forced sync between primary and secondary storage")
        sync_start_time = time.time()

        try:
            # Get all memories from primary storage
            primary_memories = await self.primary.get_all_memories()

            # Check Cloudflare availability
            try:
                await self.secondary.get_stats()  # Simple health check
                cloudflare_available = True
            except Exception as e:
                logger.warning(f"Cloudflare not available during force sync: {e}")
                cloudflare_available = False
                self.sync_stats['cloudflare_available'] = False
                return {
                    'status': 'partial',
                    'cloudflare_available': False,
                    'primary_memories': len(primary_memories),
                    'synced_to_secondary': 0,
                    'duration': time.time() - sync_start_time
                }

            # Sync from primary to secondary using concurrent operations
            async def sync_memory(memory):
                try:
                    success, message = await self.secondary.store(memory)
                    if success:
                        return True, None
                    else:
                        logger.debug(f"Failed to sync memory to secondary: {message}")
                        return False, message
                except Exception as e:
                    logger.debug(f"Exception syncing memory to secondary: {e}")
                    return False, str(e)

            # Process memories concurrently in batches
            synced_count = 0
            failed_count = 0

            # Process in batches to avoid overwhelming the system
            batch_size = min(self.batch_size, 10)  # Limit concurrent operations
            for i in range(0, len(primary_memories), batch_size):
                batch = primary_memories[i:i + batch_size]
                results = await asyncio.gather(*[sync_memory(m) for m in batch], return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        failed_count += 1
                        logger.debug(f"Exception in batch sync: {result}")
                    elif isinstance(result, tuple):
                        success, _ = result
                        if success:
                            synced_count += 1
                        else:
                            failed_count += 1

            sync_duration = time.time() - sync_start_time
            self.sync_stats['last_sync_duration'] = sync_duration
            self.sync_stats['cloudflare_available'] = cloudflare_available

            logger.info(f"Force sync completed: {synced_count} synced, {failed_count} failed in {sync_duration:.2f}s")

            return {
                'status': 'completed',
                'cloudflare_available': cloudflare_available,
                'primary_memories': len(primary_memories),
                'synced_to_secondary': synced_count,
                'failed_operations': failed_count,
                'duration': sync_duration
            }

        except Exception as e:
            logger.error(f"Error during force sync: {e}")
            return {
                'status': 'error',
                'error': str(e),
                'duration': time.time() - sync_start_time
            }

    async def get_sync_status(self) -> Dict[str, Any]:
        """Get current sync service status and statistics."""
        queue_size = self.operation_queue.qsize()

        status = {
            'is_running': self.is_running,
            'queue_size': queue_size,
            'failed_operations': len(self.failed_operations),
            'last_sync_time': self.last_sync_time,
            'consecutive_failures': self.consecutive_failures,
            'stats': self.sync_stats.copy(),
            'cloudflare_available': self.sync_stats['cloudflare_available'],
            'next_sync_in': max(0, self.sync_interval - (time.time() - self.last_sync_time)),
            'capacity': {
                'vector_count': self.cloudflare_stats['vector_count'],
                'vector_limit': CLOUDFLARE_VECTORIZE_MAX_VECTORS,
                'approaching_limits': self.cloudflare_stats['approaching_limits'],
                'warnings': self.cloudflare_stats['limit_warnings']
            }
        }

        return status

    async def validate_memory_for_cloudflare(self, memory: Memory) -> Tuple[bool, Optional[str]]:
        """
        Validate if a memory can be synced to Cloudflare.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check metadata size
        if memory.metadata:
            import json
            metadata_json = json.dumps(memory.metadata)
            metadata_size_kb = len(metadata_json.encode('utf-8')) / 1024

            if metadata_size_kb > CLOUDFLARE_MAX_METADATA_SIZE_KB:
                return False, f"Metadata size {metadata_size_kb:.2f}KB exceeds Cloudflare limit of {CLOUDFLARE_MAX_METADATA_SIZE_KB}KB"

        # Check if we're approaching vector count limit
        if self.cloudflare_stats['vector_count'] >= CLOUDFLARE_VECTORIZE_MAX_VECTORS:
            return False, f"Cloudflare vector limit of {CLOUDFLARE_VECTORIZE_MAX_VECTORS} reached"

        return True, None

    async def check_cloudflare_capacity(self) -> Dict[str, Any]:
        """
        Check remaining Cloudflare capacity and return status.
        """
        try:
            # Get current stats from Cloudflare
            cf_stats = await self.secondary.get_stats()

            # Update our tracking
            self.cloudflare_stats['vector_count'] = cf_stats.get('total_memories', 0)
            self.cloudflare_stats['last_capacity_check'] = time.time()

            # Calculate usage percentages
            vector_usage_percent = (self.cloudflare_stats['vector_count'] / CLOUDFLARE_VECTORIZE_MAX_VECTORS) * 100

            # Clear previous warnings
            self.cloudflare_stats['limit_warnings'] = []

            # Check vector count limits
            if vector_usage_percent >= CLOUDFLARE_CRITICAL_THRESHOLD_PERCENT:
                warning = f"CRITICAL: Vector usage at {vector_usage_percent:.1f}% ({self.cloudflare_stats['vector_count']:,}/{CLOUDFLARE_VECTORIZE_MAX_VECTORS:,})"
                self.cloudflare_stats['limit_warnings'].append(warning)
                logger.error(warning)
                self.cloudflare_stats['approaching_limits'] = True
            elif vector_usage_percent >= CLOUDFLARE_WARNING_THRESHOLD_PERCENT:
                warning = f"WARNING: Vector usage at {vector_usage_percent:.1f}% ({self.cloudflare_stats['vector_count']:,}/{CLOUDFLARE_VECTORIZE_MAX_VECTORS:,})"
                self.cloudflare_stats['limit_warnings'].append(warning)
                logger.warning(warning)
                self.cloudflare_stats['approaching_limits'] = True
            else:
                self.cloudflare_stats['approaching_limits'] = False

            return {
                'vector_count': self.cloudflare_stats['vector_count'],
                'vector_limit': CLOUDFLARE_VECTORIZE_MAX_VECTORS,
                'vector_usage_percent': vector_usage_percent,
                'approaching_limits': self.cloudflare_stats['approaching_limits'],
                'warnings': self.cloudflare_stats['limit_warnings']
            }

        except Exception as e:
            logger.error(f"Failed to check Cloudflare capacity: {e}")
            return {
                'error': str(e),
                'approaching_limits': False
            }

    async def _sync_loop(self):
        """Main background sync loop."""
        logger.info("Background sync loop started")

        while self.is_running:
            try:
                # Process queued operations
                await self._process_operation_queue()

                # Periodic full sync if enough time has passed
                current_time = time.time()
                if current_time - self.last_sync_time >= self.sync_interval:
                    await self._periodic_sync()
                    self.last_sync_time = current_time

                # Sleep before next iteration
                await asyncio.sleep(5)  # Check every 5 seconds

            except Exception as e:
                logger.error(f"Error in sync loop: {e}")
                self.consecutive_failures += 1

                if self.consecutive_failures >= self.max_consecutive_failures:
                    logger.warning(f"Too many consecutive sync failures ({self.consecutive_failures}), backing off for {self.backoff_time}s")
                    await asyncio.sleep(self.backoff_time)
                    self.backoff_time = min(self.backoff_time * 2, 1800)  # Max 30 minutes
                else:
                    await asyncio.sleep(1)

    async def _process_operation_queue(self):
        """Process operations from the queue in batches."""
        operations = []

        # Collect up to batch_size operations
        for _ in range(self.batch_size):
            try:
                operation = self.operation_queue.get_nowait()
                operations.append(operation)
            except asyncio.QueueEmpty:
                break

        if operations:
            await self._process_operations_batch(operations)

    async def _process_operations_batch(self, operations: List[SyncOperation]):
        """Process a batch of sync operations."""
        logger.debug(f"Processing batch of {len(operations)} sync operations")

        for operation in operations:
            try:
                await self._process_single_operation(operation)
                self.sync_stats['operations_processed'] += 1

            except Exception as e:
                await self._handle_sync_error(e, operation)

    async def _handle_sync_error(self, error: Exception, operation: SyncOperation):
        """
        Handle sync operation errors with intelligent retry logic.

        Args:
            error: The exception that occurred
            operation: The failed operation
        """
        error_str = str(error).lower()

        # Check for specific Cloudflare limit errors
        is_limit_error = any(term in error_str for term in [
            'limit exceeded', 'quota exceeded', 'maximum', 'too large',
            '413', '507', 'insufficient storage', 'capacity'
        ])

        if is_limit_error:
            # Don't retry limit errors - they won't succeed
            logger.error(f"Cloudflare limit error for {operation.operation}: {error}")
            self.sync_stats['operations_failed'] += 1

            # Update capacity tracking
            self.cloudflare_stats['approaching_limits'] = True
            self.cloudflare_stats['limit_warnings'].append(f"Limit error: {error}")

            # Check capacity to understand the issue
            await self.check_cloudflare_capacity()
            return

        # Check for temporary/network errors
        is_temporary_error = any(term in error_str for term in [
            'timeout', 'connection', 'network', '500', '502', '503', '504',
            'temporarily unavailable', 'retry'
        ])

        if is_temporary_error or operation.retries < operation.max_retries:
            # Retry temporary errors
            logger.warning(f"Temporary error for {operation.operation} (retry {operation.retries + 1}/{operation.max_retries}): {error}")
            operation.retries += 1

            if operation.retries < operation.max_retries:
                # Add back to queue for retry with exponential backoff
                await asyncio.sleep(min(2 ** operation.retries, 60))  # Max 60 second delay
                self.failed_operations.append(operation)
            else:
                logger.error(f"Max retries reached for {operation.operation}")
                self.sync_stats['operations_failed'] += 1
        else:
            # Permanent error - don't retry
            logger.error(f"Permanent error for {operation.operation}: {error}")
            self.sync_stats['operations_failed'] += 1

    async def _process_single_operation(self, operation: SyncOperation):
        """Process a single sync operation to secondary storage."""
        try:
            if operation.operation == 'store' and operation.memory:
                # Validate memory before syncing
                is_valid, validation_error = await self.validate_memory_for_cloudflare(operation.memory)
                if not is_valid:
                    logger.warning(f"Memory validation failed for sync: {validation_error}")
                    # Don't retry if it's a hard limit
                    if "exceeds Cloudflare limit" in validation_error or "limit of" in validation_error:
                        self.sync_stats['operations_failed'] += 1
                        return  # Skip this memory permanently
                    raise Exception(validation_error)

                success, message = await self.secondary.store(operation.memory)
                if not success:
                    raise Exception(f"Store operation failed: {message}")

            elif operation.operation == 'delete' and operation.content_hash:
                success, message = await self.secondary.delete(operation.content_hash)
                if not success:
                    raise Exception(f"Delete operation failed: {message}")

            elif operation.operation == 'update' and operation.content_hash and operation.updates:
                success, message = await self.secondary.update_memory_metadata(
                    operation.content_hash, operation.updates
                )
                if not success:
                    raise Exception(f"Update operation failed: {message}")

            # Reset failure counters on success
            self.consecutive_failures = 0
            self.backoff_time = 60
            self.sync_stats['cloudflare_available'] = True

        except Exception as e:
            # Mark Cloudflare as potentially unavailable
            self.sync_stats['cloudflare_available'] = False
            raise

    async def _periodic_sync(self):
        """Perform periodic full synchronization."""
        logger.debug("Starting periodic sync")

        try:
            # Retry any failed operations first
            if self.failed_operations:
                retry_operations = list(self.failed_operations)
                self.failed_operations.clear()
                logger.info(f"Retrying {len(retry_operations)} failed operations")
                await self._process_operations_batch(retry_operations)

            # Perform a lightweight health check
            try:
                stats = await self.secondary.get_stats()
                logger.debug(f"Secondary storage health check passed: {stats}")
                self.sync_stats['cloudflare_available'] = True

                # Check Cloudflare capacity every periodic sync
                capacity_status = await self.check_cloudflare_capacity()
                if capacity_status.get('approaching_limits'):
                    logger.warning("Cloudflare approaching capacity limits")
                    for warning in capacity_status.get('warnings', []):
                        logger.warning(warning)

            except Exception as e:
                logger.warning(f"Secondary storage health check failed: {e}")
                self.sync_stats['cloudflare_available'] = False

        except Exception as e:
            logger.error(f"Error during periodic sync: {e}")


class HybridMemoryStorage(MemoryStorage):
    """
    Hybrid memory storage using SQLite-vec as primary and Cloudflare as secondary.

    This implementation provides:
    - Ultra-fast reads and writes (~5ms) via SQLite-vec
    - Cloud persistence and multi-device sync via Cloudflare
    - Background synchronization with retry logic
    - Graceful degradation when cloud services are unavailable
    - Full compatibility with the MemoryStorage interface
    """

    def __init__(self,
                 sqlite_db_path: str,
                 embedding_model: str = "all-MiniLM-L6-v2",
                 cloudflare_config: Dict[str, Any] = None,
                 sync_interval: int = 300,
                 batch_size: int = 50):
        """
        Initialize hybrid storage with primary SQLite-vec and secondary Cloudflare.

        Args:
            sqlite_db_path: Path to SQLite-vec database file
            embedding_model: Embedding model name for SQLite-vec
            cloudflare_config: Cloudflare configuration dict
            sync_interval: Background sync interval in seconds (default: 5 minutes)
            batch_size: Batch size for sync operations (default: 50)
        """
        self.primary = SqliteVecMemoryStorage(
            db_path=sqlite_db_path,
            embedding_model=embedding_model
        )

        # Initialize Cloudflare storage if config provided
        self.secondary = None
        self.sync_service = None

        if cloudflare_config and all(key in cloudflare_config for key in
                                    ['api_token', 'account_id', 'vectorize_index', 'd1_database_id']):
            self.secondary = CloudflareStorage(**cloudflare_config)
        else:
            logger.warning("Cloudflare config incomplete, running in SQLite-only mode")

        self.sync_interval = sync_interval
        self.batch_size = batch_size
        self.initialized = False

    async def initialize(self) -> None:
        """Initialize the hybrid storage system."""
        logger.info("Initializing hybrid memory storage...")

        # Always initialize primary storage
        await self.primary.initialize()
        logger.info("Primary storage (SQLite-vec) initialized")

        # Initialize secondary storage and sync service if available
        if self.secondary:
            try:
                await self.secondary.initialize()
                logger.info("Secondary storage (Cloudflare) initialized")

                # Start background sync service
                self.sync_service = BackgroundSyncService(
                    self.primary,
                    self.secondary,
                    sync_interval=self.sync_interval,
                    batch_size=self.batch_size
                )
                await self.sync_service.start()
                logger.info("Background sync service started")

            except Exception as e:
                logger.warning(f"Failed to initialize secondary storage: {e}")
                self.secondary = None

        self.initialized = True
        logger.info("Hybrid memory storage initialization completed")

    async def store(self, memory: Memory) -> Tuple[bool, str]:
        """Store a memory in primary storage and queue for secondary sync."""
        # Always store in primary first for immediate availability
        success, message = await self.primary.store(memory)

        if success and self.sync_service:
            # Queue for background sync to secondary
            operation = SyncOperation(operation='store', memory=memory)
            await self.sync_service.enqueue_operation(operation)

        return success, message

    async def retrieve(self, query: str, n_results: int = 5) -> List[MemoryQueryResult]:
        """Retrieve memories from primary storage (fast)."""
        return await self.primary.retrieve(query, n_results)

    async def search(self, query: str, n_results: int = 5, min_similarity: float = 0.0) -> List[MemoryQueryResult]:
        """Search memories in primary storage."""
        return await self.primary.search(query, n_results)

    async def search_by_tag(self, tags: List[str], match_all: bool = False) -> List[Memory]:
        """Search memories by tags in primary storage."""
        operation = "AND" if match_all else "OR"
        return await self.primary.search_by_tags(tags, operation=operation)

    async def search_by_tags(self, tags: List[str], match_all: bool = False) -> List[Memory]:
        """Search memories by tags (alternative method signature)."""
        operation = "AND" if match_all else "OR"
        return await self.primary.search_by_tags(tags, operation=operation)

    async def delete(self, content_hash: str) -> Tuple[bool, str]:
        """Delete a memory from primary storage and queue for secondary sync."""
        success, message = await self.primary.delete(content_hash)

        if success and self.sync_service:
            # Queue for background sync to secondary
            operation = SyncOperation(operation='delete', content_hash=content_hash)
            await self.sync_service.enqueue_operation(operation)

        return success, message

    async def delete_by_tag(self, tag: str) -> Tuple[int, str]:
        """Delete memories by tag from primary storage and queue for secondary sync."""
        # First, get the memories with this tag to get their hashes for sync
        memories_to_delete = await self.primary.search_by_tags([tag])

        # Delete from primary
        count_deleted, message = await self.primary.delete_by_tag(tag)

        # Queue individual deletes for secondary sync
        if count_deleted > 0 and self.sync_service:
            for memory in memories_to_delete:
                operation = SyncOperation(operation='delete', content_hash=memory.content_hash)
                await self.sync_service.enqueue_operation(operation)

        return count_deleted, message

    async def cleanup_duplicates(self) -> Tuple[int, str]:
        """Clean up duplicates in primary storage."""
        # Only cleanup primary, secondary will sync naturally
        return await self.primary.cleanup_duplicates()

    async def update_memory_metadata(self, content_hash: str, updates: Dict[str, Any], preserve_timestamps: bool = True) -> Tuple[bool, str]:
        """Update memory metadata in primary storage and queue for secondary sync."""
        success, message = await self.primary.update_memory_metadata(content_hash, updates, preserve_timestamps)

        if success and self.sync_service:
            # Queue for background sync to secondary
            operation = SyncOperation(
                operation='update',
                content_hash=content_hash,
                updates=updates
            )
            await self.sync_service.enqueue_operation(operation)

        return success, message

    async def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics from both storage backends."""
        # SQLite-vec get_stats is synchronous, not async
        primary_stats = self.primary.get_stats()

        stats = {
            "storage_backend": "Hybrid (SQLite-vec + Cloudflare)",
            "primary_backend": "SQLite-vec",
            "secondary_backend": "Cloudflare" if self.secondary else "None",
            "total_memories": primary_stats.get("total_memories", 0),
            "primary_stats": primary_stats,
            "sync_enabled": self.sync_service is not None
        }

        # Add sync service statistics if available
        if self.sync_service:
            sync_status = await self.sync_service.get_sync_status()
            stats["sync_status"] = sync_status

        # Add secondary stats if available and healthy
        if self.secondary and self.sync_service and self.sync_service.sync_stats['cloudflare_available']:
            try:
                secondary_stats = await self.secondary.get_stats()
                stats["secondary_stats"] = secondary_stats
            except Exception as e:
                stats["secondary_error"] = str(e)

        return stats

    async def get_all_tags(self) -> List[str]:
        """Get all unique tags from primary storage."""
        return self.primary.get_all_tags()

    async def get_recent_memories(self, n: int = 10) -> List[Memory]:
        """Get recent memories from primary storage."""
        return self.primary.get_recent_memories(n)

    async def recall_memory(self, query: str, n_results: int = 5) -> List[Memory]:
        """Recall memories using natural language time expressions."""
        return self.primary.recall_memory(query, n_results)

    async def get_all_memories(self, limit: int = None, offset: int = 0, memory_type: Optional[str] = None, tags: Optional[List[str]] = None) -> List[Memory]:
        """Get all memories from primary storage."""
        return await self.primary.get_all_memories(limit=limit, offset=offset, memory_type=memory_type, tags=tags)

    async def count_all_memories(self, memory_type: Optional[str] = None) -> int:
        """Get total count of memories from primary storage."""
        return await self.primary.count_all_memories(memory_type=memory_type)

    async def get_memories_by_time_range(self, start_time: float, end_time: float) -> List[Memory]:
        """Get memories within time range from primary storage."""
        return self.primary.get_memories_by_time_range(start_time, end_time)

    async def close(self):
        """Clean shutdown of hybrid storage system."""
        logger.info("Shutting down hybrid memory storage...")

        # Stop sync service first
        if self.sync_service:
            await self.sync_service.stop()

        # Close storage backends
        if hasattr(self.primary, 'close') and self.primary.close:
            if asyncio.iscoroutinefunction(self.primary.close):
                await self.primary.close()
            else:
                self.primary.close()

        if self.secondary and hasattr(self.secondary, 'close') and self.secondary.close:
            if asyncio.iscoroutinefunction(self.secondary.close):
                await self.secondary.close()
            else:
                self.secondary.close()

        logger.info("Hybrid memory storage shutdown completed")

    async def force_sync(self) -> Dict[str, Any]:
        """Force immediate synchronization with secondary storage."""
        if not self.sync_service:
            return {
                'status': 'disabled',
                'message': 'Background sync service not available'
            }

        return await self.sync_service.force_sync()