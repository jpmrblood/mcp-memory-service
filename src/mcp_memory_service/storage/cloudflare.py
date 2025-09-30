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
Cloudflare storage backend for MCP Memory Service.
Provides cloud-native storage using Vectorize, D1, and R2.
"""

import json
import logging
import hashlib
import asyncio
import time
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime
import httpx

from .base import MemoryStorage
from ..models.memory import Memory, MemoryQueryResult
from ..utils.hashing import generate_content_hash

logger = logging.getLogger(__name__)

class CloudflareStorage(MemoryStorage):
    """Cloudflare-based storage backend using Vectorize, D1, and R2."""
    
    def __init__(self, 
                 api_token: str,
                 account_id: str,
                 vectorize_index: str,
                 d1_database_id: str,
                 r2_bucket: Optional[str] = None,
                 embedding_model: str = "@cf/baai/bge-base-en-v1.5",
                 large_content_threshold: int = 1024 * 1024,  # 1MB
                 max_retries: int = 3,
                 base_delay: float = 1.0):
        """
        Initialize Cloudflare storage backend.
        
        Args:
            api_token: Cloudflare API token
            account_id: Cloudflare account ID
            vectorize_index: Vectorize index name
            d1_database_id: D1 database ID
            r2_bucket: Optional R2 bucket for large content
            embedding_model: Workers AI embedding model
            large_content_threshold: Size threshold for R2 storage
            max_retries: Maximum retry attempts for API calls
            base_delay: Base delay for exponential backoff
        """
        self.api_token = api_token
        self.account_id = account_id
        self.vectorize_index = vectorize_index
        self.d1_database_id = d1_database_id
        self.r2_bucket = r2_bucket
        self.embedding_model = embedding_model
        self.large_content_threshold = large_content_threshold
        self.max_retries = max_retries
        self.base_delay = base_delay
        
        # API endpoints
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        self.vectorize_url = f"{self.base_url}/vectorize/v2/indexes/{vectorize_index}"
        self.d1_url = f"{self.base_url}/d1/database/{d1_database_id}"
        self.ai_url = f"{self.base_url}/ai/run/{embedding_model}"
        
        if r2_bucket:
            self.r2_url = f"{self.base_url}/r2/buckets/{r2_bucket}/objects"
        
        # HTTP client with connection pooling
        self.client = None
        self._initialized = False
        
        # Embedding cache for performance
        self._embedding_cache = {}
        self._cache_max_size = 1000
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with connection pooling."""
        if self.client is None:
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json"
            }
            self.client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(30.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
            )
        return self.client
    
    async def _retry_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make HTTP request with exponential backoff retry logic."""
        client = await self._get_client()
        
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.request(method, url, **kwargs)
                
                # Handle rate limiting
                if response.status_code == 429:
                    if attempt < self.max_retries:
                        delay = self.base_delay * (2 ** attempt)
                        logger.warning(f"Rate limited, retrying in {delay}s (attempt {attempt + 1}/{self.max_retries + 1})")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        raise httpx.HTTPError(f"Rate limited after {self.max_retries} retries")
                
                # Handle server errors
                if response.status_code >= 500:
                    if attempt < self.max_retries:
                        delay = self.base_delay * (2 ** attempt)
                        logger.warning(f"Server error {response.status_code}, retrying in {delay}s")
                        await asyncio.sleep(delay)
                        continue
                
                response.raise_for_status()
                return response
                
            except (httpx.NetworkError, httpx.TimeoutException) as e:
                if attempt < self.max_retries:
                    delay = self.base_delay * (2 ** attempt)
                    logger.warning(f"Network error: {e}, retrying in {delay}s")
                    await asyncio.sleep(delay)
                    continue
                raise
        
        raise httpx.HTTPError(f"Failed after {self.max_retries} retries")
    
    async def _generate_embedding(self, text: str) -> List[float]:
        """Generate embedding using Workers AI or cache."""
        # Check cache first
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        if text_hash in self._embedding_cache:
            return self._embedding_cache[text_hash]
        
        try:
            # Use Workers AI to generate embedding
            payload = {"text": [text]}
            response = await self._retry_request("POST", self.ai_url, json=payload)
            result = response.json()
            
            if result.get("success") and "result" in result:
                embedding = result["result"]["data"][0]
                
                # Cache the embedding (with size limit)
                if len(self._embedding_cache) >= self._cache_max_size:
                    # Remove oldest entry (simple FIFO)
                    oldest_key = next(iter(self._embedding_cache))
                    del self._embedding_cache[oldest_key]
                
                self._embedding_cache[text_hash] = embedding
                return embedding
            else:
                raise ValueError(f"Workers AI embedding failed: {result}")
                
        except Exception as e:
            logger.error(f"Failed to generate embedding with Workers AI: {e}")
            # TODO: Implement fallback to local sentence-transformers
            raise ValueError(f"Embedding generation failed: {e}")
    
    async def initialize(self) -> None:
        """Initialize the Cloudflare storage backend."""
        if self._initialized:
            return
        
        logger.info("Initializing Cloudflare storage backend...")
        
        try:
            # Initialize D1 database schema
            await self._initialize_d1_schema()
            
            # Verify Vectorize index exists
            await self._verify_vectorize_index()
            
            # Verify R2 bucket if configured
            if self.r2_bucket:
                await self._verify_r2_bucket()
            
            self._initialized = True
            logger.info("Cloudflare storage backend initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize Cloudflare storage: {e}")
            raise
    
    async def _initialize_d1_schema(self) -> None:
        """Initialize D1 database schema."""
        schema_sql = """
        -- Memory metadata table
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            memory_type TEXT,
            created_at REAL NOT NULL,
            created_at_iso TEXT NOT NULL,
            updated_at REAL,
            updated_at_iso TEXT,
            metadata_json TEXT,
            vector_id TEXT UNIQUE,
            content_size INTEGER DEFAULT 0,
            r2_key TEXT
        );
        
        -- Tags table
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        
        -- Memory-tag relationships
        CREATE TABLE IF NOT EXISTS memory_tags (
            memory_id INTEGER,
            tag_id INTEGER,
            PRIMARY KEY (memory_id, tag_id),
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );
        
        -- Indexes for performance
        CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash);
        CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
        CREATE INDEX IF NOT EXISTS idx_memories_vector_id ON memories(vector_id);
        CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
        """
        
        payload = {"sql": schema_sql}
        response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
        result = response.json()
        
        if not result.get("success"):
            raise ValueError(f"Failed to initialize D1 schema: {result}")
    
    async def _verify_vectorize_index(self) -> None:
        """Verify Vectorize index exists and is accessible."""
        try:
            response = await self._retry_request("GET", f"{self.vectorize_url}")
            result = response.json()
            
            if not result.get("success"):
                raise ValueError(f"Vectorize index not accessible: {result}")
                
            logger.info(f"Vectorize index verified: {self.vectorize_index}")
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ValueError(f"Vectorize index '{self.vectorize_index}' not found")
            raise
    
    async def _verify_r2_bucket(self) -> None:
        """Verify R2 bucket exists and is accessible."""
        try:
            # Try to list objects (empty list is fine)
            response = await self._retry_request("GET", f"{self.r2_url}?max-keys=1")
            logger.info(f"R2 bucket verified: {self.r2_bucket}")
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ValueError(f"R2 bucket '{self.r2_bucket}' not found")
            raise
    
    async def store(self, memory: Memory) -> Tuple[bool, str]:
        """Store a memory in Cloudflare storage."""
        try:
            # Generate embedding for the content
            embedding = await self._generate_embedding(memory.content)
            
            # Determine storage strategy based on content size
            content_size = len(memory.content.encode('utf-8'))
            use_r2 = self.r2_bucket and content_size > self.large_content_threshold
            
            # Store large content in R2 if needed
            r2_key = None
            stored_content = memory.content
            
            if use_r2:
                r2_key = f"content/{memory.content_hash}.txt"
                await self._store_r2_content(r2_key, memory.content)
                stored_content = f"[R2 Content: {r2_key}]"  # Placeholder in D1
            
            # Store vector in Vectorize
            vector_id = memory.content_hash
            vector_metadata = {
                "content_hash": memory.content_hash,
                "memory_type": memory.memory_type or "standard",
                "tags": ",".join(memory.tags) if memory.tags else "",
                "created_at": memory.created_at_iso or datetime.now().isoformat()
            }
            
            await self._store_vectorize_vector(vector_id, embedding, vector_metadata)
            
            # Store metadata in D1
            await self._store_d1_memory(memory, vector_id, content_size, r2_key, stored_content)
            
            logger.info(f"Successfully stored memory: {memory.content_hash}")
            return True, f"Memory stored successfully (vector_id: {vector_id})"
            
        except Exception as e:
            logger.error(f"Failed to store memory {memory.content_hash}: {e}")
            return False, f"Storage failed: {str(e)}"
    
    async def _store_vectorize_vector(self, vector_id: str, embedding: List[float], metadata: Dict[str, Any]) -> None:
        """Store vector in Vectorize."""
        # Try without namespace first to isolate the issue
        vector_data = {
            "id": vector_id,
            "values": embedding,
            "metadata": metadata
        }
        
        # Convert to NDJSON format as required by the HTTP API
        import json
        ndjson_content = json.dumps(vector_data) + "\n"
        
        try:
            # Send as raw NDJSON data with correct Content-Type header
            client = await self._get_client()
            
            # Override headers for this specific request
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/x-ndjson"
            }
            
            response = await client.post(
                f"{self.vectorize_url}/upsert",
                content=ndjson_content.encode("utf-8"),
                headers=headers
            )
            
            # Log response status for debugging (avoid logging headers/body for security)
            logger.info(f"Vectorize response status: {response.status_code}")
            response_text = response.text
            if response.status_code != 200:
                # Only log response body on errors, and truncate to avoid credential exposure
                truncated_response = response_text[:200] + "..." if len(response_text) > 200 else response_text
                logger.warning(f"Vectorize error response (truncated): {truncated_response}")
            
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}: {response_text}")
            
            result = response.json()
            if not result.get("success"):
                raise ValueError(f"Failed to store vector: {result}")
                
        except Exception as e:
            # Add more detailed error logging
            logger.error(f"Vectorize insert failed: {e}")
            logger.error(f"Vector data was: {vector_data}")
            logger.error(f"NDJSON content: {ndjson_content.strip()}")
            logger.error(f"URL was: {self.vectorize_url}/upsert")
            raise ValueError(f"Failed to store vector: {e}")
    
    async def _store_d1_memory(self, memory: Memory, vector_id: str, content_size: int, r2_key: Optional[str], stored_content: str) -> None:
        """Store memory metadata in D1."""
        # Insert memory record
        insert_sql = """
        INSERT INTO memories (
            content_hash, content, memory_type, created_at, created_at_iso,
            updated_at, updated_at_iso, metadata_json, vector_id, content_size, r2_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        now = time.time()
        now_iso = datetime.now().isoformat()
        
        params = [
            memory.content_hash,
            stored_content,
            memory.memory_type,
            memory.created_at or now,
            memory.created_at_iso or now_iso,
            memory.updated_at or now,
            memory.updated_at_iso or now_iso,
            json.dumps(memory.metadata) if memory.metadata else None,
            vector_id,
            content_size,
            r2_key
        ]
        
        payload = {"sql": insert_sql, "params": params}
        response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
        result = response.json()
        
        if not result.get("success"):
            raise ValueError(f"Failed to store memory in D1: {result}")
        
        # Store tags if present
        if memory.tags:
            memory_id = result["result"][0]["meta"]["last_row_id"]
            await self._store_d1_tags(memory_id, memory.tags)
    
    async def _store_d1_tags(self, memory_id: int, tags: List[str]) -> None:
        """Store tags for a memory in D1."""
        for tag in tags:
            # Insert tag if not exists
            tag_sql = "INSERT OR IGNORE INTO tags (name) VALUES (?)"
            payload = {"sql": tag_sql, "params": [tag]}
            await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            
            # Link tag to memory
            link_sql = """
            INSERT INTO memory_tags (memory_id, tag_id)
            SELECT ?, id FROM tags WHERE name = ?
            """
            payload = {"sql": link_sql, "params": [memory_id, tag]}
            await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
    
    async def _store_r2_content(self, key: str, content: str) -> None:
        """Store content in R2."""
        response = await self._retry_request(
            "PUT", 
            f"{self.r2_url}/{key}",
            content=content.encode('utf-8'),
            headers={"Content-Type": "text/plain"}
        )
        
        if response.status_code not in [200, 201]:
            raise ValueError(f"Failed to store content in R2: {response.status_code}")
    
    async def retrieve(self, query: str, n_results: int = 5) -> List[MemoryQueryResult]:
        """Retrieve memories by semantic search."""
        try:
            # Generate query embedding
            query_embedding = await self._generate_embedding(query)
            
            # Search Vectorize (without namespace for now)
            search_payload = {
                "vector": query_embedding,
                "topK": n_results,
                "returnMetadata": "all",
                "returnValues": False
            }
            
            response = await self._retry_request("POST", f"{self.vectorize_url}/query", json=search_payload)
            result = response.json()
            
            if not result.get("success"):
                raise ValueError(f"Vectorize query failed: {result}")
            
            matches = result.get("result", {}).get("matches", [])
            
            # Convert to MemoryQueryResult objects
            results = []
            for match in matches:
                memory = await self._load_memory_from_match(match)
                if memory:
                    query_result = MemoryQueryResult(
                        memory=memory,
                        relevance_score=match.get("score", 0.0)
                    )
                    results.append(query_result)
            
            logger.info(f"Retrieved {len(results)} memories for query")
            return results
            
        except Exception as e:
            logger.error(f"Failed to retrieve memories: {e}")
            return []
    
    async def _load_memory_from_match(self, match: Dict[str, Any]) -> Optional[Memory]:
        """Load full memory from Vectorize match."""
        try:
            vector_id = match.get("id")
            metadata = match.get("metadata", {})
            content_hash = metadata.get("content_hash")
            
            if not content_hash:
                logger.warning(f"No content_hash in vector metadata: {vector_id}")
                return None
            
            # Load from D1
            sql = "SELECT * FROM memories WHERE content_hash = ?"
            payload = {"sql": sql, "params": [content_hash]}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            if not result.get("success") or not result.get("result", [{}])[0].get("results"):
                logger.warning(f"Memory not found in D1: {content_hash}")
                return None
            
            row = result["result"][0]["results"][0]
            
            # Load content from R2 if needed
            content = row["content"]
            if row.get("r2_key") and content.startswith("[R2 Content:"):
                content = await self._load_r2_content(row["r2_key"])
            
            # Load tags
            tags = await self._load_memory_tags(row["id"])
            
            # Reconstruct Memory object
            memory = Memory(
                content=content,
                content_hash=content_hash,
                tags=tags,
                memory_type=row.get("memory_type"),
                metadata=json.loads(row["metadata_json"]) if row.get("metadata_json") else {},
                created_at=row.get("created_at"),
                created_at_iso=row.get("created_at_iso"),
                updated_at=row.get("updated_at"),
                updated_at_iso=row.get("updated_at_iso")
            )
            
            return memory
            
        except Exception as e:
            logger.error(f"Failed to load memory from match: {e}")
            return None
    
    async def _load_r2_content(self, r2_key: str) -> str:
        """Load content from R2."""
        response = await self._retry_request("GET", f"{self.r2_url}/{r2_key}")
        return response.text
    
    async def _load_memory_tags(self, memory_id: int) -> List[str]:
        """Load tags for a memory from D1."""
        sql = """
        SELECT t.name FROM tags t
        JOIN memory_tags mt ON t.id = mt.tag_id
        WHERE mt.memory_id = ?
        """
        payload = {"sql": sql, "params": [memory_id]}
        response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
        result = response.json()
        
        if result.get("success") and result.get("result", [{}])[0].get("results"):
            return [row["name"] for row in result["result"][0]["results"]]
        
        return []
    
    async def search_by_tag(self, tags: List[str]) -> List[Memory]:
        """Search memories by tags."""
        try:
            if not tags:
                return []
            
            # Build SQL query for tag search
            placeholders = ",".join(["?"] * len(tags))
            sql = f"""
            SELECT DISTINCT m.* FROM memories m
            JOIN memory_tags mt ON m.id = mt.memory_id
            JOIN tags t ON mt.tag_id = t.id
            WHERE t.name IN ({placeholders})
            ORDER BY m.created_at DESC
            """
            
            payload = {"sql": sql, "params": tags}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            if not result.get("success"):
                raise ValueError(f"D1 tag search failed: {result}")
            
            memories = []
            if result.get("result", [{}])[0].get("results"):
                for row in result["result"][0]["results"]:
                    memory = await self._load_memory_from_row(row)
                    if memory:
                        memories.append(memory)
            
            logger.info(f"Found {len(memories)} memories with tags: {tags}")
            return memories
            
        except Exception as e:
            logger.error(f"Failed to search by tags: {e}")
            return []
    
    async def _load_memory_from_row(self, row: Dict[str, Any]) -> Optional[Memory]:
        """Load memory from D1 row data."""
        try:
            # Load content from R2 if needed
            content = row["content"]
            if row.get("r2_key") and content.startswith("[R2 Content:"):
                content = await self._load_r2_content(row["r2_key"])
            
            # Load tags
            tags = await self._load_memory_tags(row["id"])
            
            memory = Memory(
                content=content,
                content_hash=row["content_hash"],
                tags=tags,
                memory_type=row.get("memory_type"),
                metadata=json.loads(row["metadata_json"]) if row.get("metadata_json") else {},
                created_at=row.get("created_at"),
                created_at_iso=row.get("created_at_iso"),
                updated_at=row.get("updated_at"),
                updated_at_iso=row.get("updated_at_iso")
            )
            
            return memory
            
        except Exception as e:
            logger.error(f"Failed to load memory from row: {e}")
            return None
    
    async def delete(self, content_hash: str) -> Tuple[bool, str]:
        """Delete a memory by its hash."""
        try:
            # Find memory in D1
            sql = "SELECT * FROM memories WHERE content_hash = ?"
            payload = {"sql": sql, "params": [content_hash]}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            if not result.get("success") or not result.get("result", [{}])[0].get("results"):
                return False, f"Memory not found: {content_hash}"
            
            row = result["result"][0]["results"][0]
            memory_id = row["id"]
            vector_id = row.get("vector_id")
            r2_key = row.get("r2_key")
            
            # Delete from Vectorize
            if vector_id:
                await self._delete_vectorize_vector(vector_id)
            
            # Delete from R2 if present
            if r2_key:
                await self._delete_r2_content(r2_key)
            
            # Delete from D1 (tags will be cascade deleted)
            delete_sql = "DELETE FROM memories WHERE id = ?"
            payload = {"sql": delete_sql, "params": [memory_id]}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            if not result.get("success"):
                raise ValueError(f"Failed to delete from D1: {result}")
            
            logger.info(f"Successfully deleted memory: {content_hash}")
            return True, "Memory deleted successfully"
            
        except Exception as e:
            logger.error(f"Failed to delete memory {content_hash}: {e}")
            return False, f"Deletion failed: {str(e)}"
    
    async def _delete_vectorize_vector(self, vector_id: str) -> None:
        """Delete vector from Vectorize."""
        # Send IDs directly in the payload
        payload = [vector_id]
        
        response = await self._retry_request("POST", f"{self.vectorize_url}/delete-by-ids", json=payload)
        result = response.json()
        
        if not result.get("success"):
            logger.warning(f"Failed to delete vector from Vectorize: {result}")
    
    async def _delete_r2_content(self, r2_key: str) -> None:
        """Delete content from R2."""
        try:
            response = await self._retry_request("DELETE", f"{self.r2_url}/{r2_key}")
            if response.status_code not in [200, 204, 404]:  # 404 is fine if already deleted
                logger.warning(f"Failed to delete R2 content: {response.status_code}")
        except Exception as e:
            logger.warning(f"Failed to delete R2 content {r2_key}: {e}")
    
    async def delete_by_tag(self, tag: str) -> Tuple[int, str]:
        """Delete memories by tag."""
        try:
            # Find memories with the tag
            memories = await self.search_by_tag([tag])
            
            deleted_count = 0
            for memory in memories:
                success, _ = await self.delete(memory.content_hash)
                if success:
                    deleted_count += 1
            
            logger.info(f"Deleted {deleted_count} memories with tag: {tag}")
            return deleted_count, f"Deleted {deleted_count} memories"
            
        except Exception as e:
            logger.error(f"Failed to delete by tag {tag}: {e}")
            return 0, f"Deletion failed: {str(e)}"
    
    async def cleanup_duplicates(self) -> Tuple[int, str]:
        """Remove duplicate memories based on content hash."""
        try:
            # Find duplicates in D1
            sql = """
            SELECT content_hash, COUNT(*) as count, MIN(id) as keep_id
            FROM memories
            GROUP BY content_hash
            HAVING COUNT(*) > 1
            """
            
            payload = {"sql": sql}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            if not result.get("success"):
                raise ValueError(f"Failed to find duplicates: {result}")
            
            duplicate_groups = result.get("result", [{}])[0].get("results", [])
            
            total_deleted = 0
            for group in duplicate_groups:
                content_hash = group["content_hash"]
                keep_id = group["keep_id"]
                
                # Delete all except the first one
                delete_sql = "DELETE FROM memories WHERE content_hash = ? AND id != ?"
                payload = {"sql": delete_sql, "params": [content_hash, keep_id]}
                response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
                result = response.json()
                
                if result.get("success") and result.get("result", [{}])[0].get("meta"):
                    deleted = result["result"][0]["meta"].get("changes", 0)
                    total_deleted += deleted
            
            logger.info(f"Cleaned up {total_deleted} duplicate memories")
            return total_deleted, f"Removed {total_deleted} duplicates"
            
        except Exception as e:
            logger.error(f"Failed to cleanup duplicates: {e}")
            return 0, f"Cleanup failed: {str(e)}"
    
    async def update_memory_metadata(self, content_hash: str, updates: Dict[str, Any], preserve_timestamps: bool = True) -> Tuple[bool, str]:
        """Update memory metadata without recreating the entry."""
        try:
            # Build update SQL
            update_fields = []
            params = []
            
            if "metadata" in updates:
                update_fields.append("metadata_json = ?")
                params.append(json.dumps(updates["metadata"]))
            
            if "memory_type" in updates:
                update_fields.append("memory_type = ?")
                params.append(updates["memory_type"])
            
            if "tags" in updates:
                # Handle tags separately - they require relational updates
                pass
            
            # Always update updated_at timestamp
            if not preserve_timestamps or "updated_at" not in updates:
                update_fields.append("updated_at = ?")
                update_fields.append("updated_at_iso = ?")
                now = time.time()
                now_iso = datetime.now().isoformat()
                params.extend([now, now_iso])
            
            if not update_fields:
                return True, "No updates needed"
            
            # Update memory record
            sql = f"UPDATE memories SET {', '.join(update_fields)} WHERE content_hash = ?"
            params.append(content_hash)
            
            payload = {"sql": sql, "params": params}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            if not result.get("success"):
                raise ValueError(f"Failed to update memory: {result}")
            
            # Handle tag updates if provided
            if "tags" in updates:
                await self._update_memory_tags(content_hash, updates["tags"])
            
            logger.info(f"Successfully updated memory metadata: {content_hash}")
            return True, "Memory metadata updated successfully"
            
        except Exception as e:
            logger.error(f"Failed to update memory metadata {content_hash}: {e}")
            return False, f"Update failed: {str(e)}"
    
    async def _update_memory_tags(self, content_hash: str, new_tags: List[str]) -> None:
        """Update tags for a memory."""
        # Get memory ID
        sql = "SELECT id FROM memories WHERE content_hash = ?"
        payload = {"sql": sql, "params": [content_hash]}
        response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
        result = response.json()
        
        if not result.get("success") or not result.get("result", [{}])[0].get("results"):
            raise ValueError(f"Memory not found: {content_hash}")
        
        memory_id = result["result"][0]["results"][0]["id"]
        
        # Delete existing tag relationships
        delete_sql = "DELETE FROM memory_tags WHERE memory_id = ?"
        payload = {"sql": delete_sql, "params": [memory_id]}
        await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
        
        # Add new tags
        if new_tags:
            await self._store_d1_tags(memory_id, new_tags)
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get storage statistics."""
        try:
            # Get memory count and size from D1
            sql = """
            SELECT 
                COUNT(*) as total_memories,
                SUM(content_size) as total_content_size,
                COUNT(DISTINCT vector_id) as total_vectors,
                COUNT(r2_key) as r2_stored_count
            FROM memories
            """
            
            payload = {"sql": sql}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            if result.get("success") and result.get("result", [{}])[0].get("results"):
                stats = result["result"][0]["results"][0]
                
                return {
                    "total_memories": stats.get("total_memories", 0),
                    "total_content_size_bytes": stats.get("total_content_size", 0),
                    "total_vectors": stats.get("total_vectors", 0),
                    "r2_stored_count": stats.get("r2_stored_count", 0),
                    "storage_backend": "cloudflare",
                    "vectorize_index": self.vectorize_index,
                    "d1_database": self.d1_database_id,
                    "r2_bucket": self.r2_bucket,
                    "status": "operational"
                }
            
            return {
                "total_memories": 0,
                "storage_backend": "cloudflare",
                "status": "operational"
            }
            
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {
                "total_memories": 0,
                "storage_backend": "cloudflare",
                "status": "error",
                "error": str(e)
            }
    
    async def get_all_tags(self) -> List[str]:
        """Get all unique tags in the storage."""
        try:
            sql = "SELECT name FROM tags ORDER BY name"
            payload = {"sql": sql}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            if result.get("success") and result.get("result", [{}])[0].get("results"):
                return [row["name"] for row in result["result"][0]["results"]]
            
            return []
            
        except Exception as e:
            logger.error(f"Failed to get all tags: {e}")
            return []
    
    async def get_recent_memories(self, n: int = 10) -> List[Memory]:
        """Get n most recent memories."""
        try:
            sql = "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?"
            payload = {"sql": sql, "params": [n]}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()
            
            memories = []
            if result.get("success") and result.get("result", [{}])[0].get("results"):
                for row in result["result"][0]["results"]:
                    memory = await self._load_memory_from_row(row)
                    if memory:
                        memories.append(memory)
            
            logger.info(f"Retrieved {len(memories)} recent memories")
            return memories
            
        except Exception as e:
            logger.error(f"Failed to get recent memories: {e}")
            return []
    
    def sanitized(self, tags):
        """Sanitize and normalize tags to a JSON string.
        
        This method provides compatibility with the ChromaMemoryStorage interface.
        """
        if tags is None:
            return json.dumps([])
        
        # If we get a string, split it into an array
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        # If we get an array, use it directly
        elif isinstance(tags, list):
            tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        else:
            return json.dumps([])
                
        # Return JSON string representation of the array
        return json.dumps(tags)
    
    async def recall(self, query: Optional[str] = None, n_results: int = 5, start_timestamp: Optional[float] = None, end_timestamp: Optional[float] = None) -> List[MemoryQueryResult]:
        """
        Retrieve memories with combined time filtering and optional semantic search.

        Args:
            query: Optional semantic search query. If None, only time filtering is applied.
            n_results: Maximum number of results to return.
            start_timestamp: Optional start time for filtering.
            end_timestamp: Optional end time for filtering.

        Returns:
            List of MemoryQueryResult objects.
        """
        try:
            # Build time filtering WHERE clause for D1
            time_conditions = []
            params = []

            if start_timestamp is not None:
                time_conditions.append("created_at >= ?")
                params.append(float(start_timestamp))

            if end_timestamp is not None:
                time_conditions.append("created_at <= ?")
                params.append(float(end_timestamp))

            time_where = " AND ".join(time_conditions) if time_conditions else ""

            logger.info(f"Recall - Time filtering conditions: {time_where}, params: {params}")

            # Determine search strategy
            if query and query.strip():
                # Combined semantic search with time filtering
                logger.info(f"Recall - Using semantic search with query: '{query}'")

                try:
                    # Generate query embedding
                    query_embedding = await self._generate_embedding(query)

                    # Search Vectorize with semantic query
                    search_payload = {
                        "vector": query_embedding,
                        "topK": n_results,
                        "returnMetadata": "all",
                        "returnValues": False
                    }

                    # Add time filtering to vectorize metadata if specified
                    if time_conditions:
                        # Note: Vectorize metadata filtering capabilities may be limited
                        # We'll filter after retrieval for now
                        logger.info("Recall - Time filtering will be applied post-retrieval from Vectorize")

                    response = await self._retry_request("POST", f"{self.vectorize_url}/query", json=search_payload)
                    result = response.json()

                    if not result.get("success"):
                        raise ValueError(f"Vectorize query failed: {result}")

                    matches = result.get("result", {}).get("matches", [])

                    # Convert matches to MemoryQueryResult objects with time filtering
                    results = []
                    for match in matches:
                        memory = await self._load_memory_from_match(match)
                        if memory:
                            # Apply time filtering if needed
                            if start_timestamp is not None and memory.created_at and memory.created_at < start_timestamp:
                                continue
                            if end_timestamp is not None and memory.created_at and memory.created_at > end_timestamp:
                                continue

                            query_result = MemoryQueryResult(
                                memory=memory,
                                relevance_score=match.get("score", 0.0)
                            )
                            results.append(query_result)

                    logger.info(f"Recall - Retrieved {len(results)} memories with semantic search and time filtering")
                    return results[:n_results]  # Ensure we don't exceed n_results

                except Exception as e:
                    logger.error(f"Recall - Semantic search failed, falling back to time-based search: {e}")
                    # Fall through to time-based search

            # Time-based search only (or fallback)
            logger.info(f"Recall - Using time-based search only")

            # Build D1 query for time-based retrieval
            if time_where:
                sql = f"SELECT * FROM memories WHERE {time_where} ORDER BY created_at DESC LIMIT ?"
                params.append(n_results)
            else:
                # No time filters, get most recent
                sql = "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?"
                params = [n_results]

            payload = {"sql": sql, "params": params}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()

            if not result.get("success"):
                raise ValueError(f"D1 query failed: {result}")

            # Convert D1 results to MemoryQueryResult objects
            results = []
            if result.get("result", [{}])[0].get("results"):
                for row in result["result"][0]["results"]:
                    memory = await self._load_memory_from_row(row)
                    if memory:
                        # For time-based search without semantic query, use timestamp as relevance
                        relevance_score = memory.created_at or 0.0
                        query_result = MemoryQueryResult(
                            memory=memory,
                            relevance_score=relevance_score
                        )
                        results.append(query_result)

            logger.info(f"Recall - Retrieved {len(results)} memories with time-based search")
            return results

        except Exception as e:
            logger.error(f"Recall failed: {e}")
            return []

    async def get_all_memories(self, limit: int = None, offset: int = 0, memory_type: Optional[str] = None, tags: Optional[List[str]] = None) -> List[Memory]:
        """
        Get all memories in storage ordered by creation time (newest first).

        Args:
            limit: Maximum number of memories to return (None for all)
            offset: Number of memories to skip (for pagination)
            memory_type: Optional filter by memory type
            tags: Optional filter by tags (matches ANY of the provided tags)

        Returns:
            List of Memory objects ordered by created_at DESC, optionally filtered by type and tags
        """
        try:
            # Build SQL query with optional memory_type and tags filters
            sql = "SELECT * FROM memories"
            params = []
            where_conditions = []

            # Add memory_type filter if specified
            if memory_type is not None:
                where_conditions.append("memory_type = ?")
                params.append(memory_type)

            # Add tags filter if specified (using LIKE for tag matching)
            if tags and len(tags) > 0:
                tag_conditions = " OR ".join(["tags LIKE ?" for _ in tags])
                where_conditions.append(f"({tag_conditions})")
                params.extend([f"%{tag}%" for tag in tags])

            # Apply WHERE clause if we have any conditions
            if where_conditions:
                sql += " WHERE " + " AND ".join(where_conditions)

            sql += " ORDER BY created_at DESC"

            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)

            if offset > 0:
                sql += " OFFSET ?"
                params.append(offset)

            payload = {"sql": sql, "params": params}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()

            if not result.get("success"):
                raise ValueError(f"D1 query failed: {result}")

            memories = []
            if result.get("result", [{}])[0].get("results"):
                for row in result["result"][0]["results"]:
                    memory = await self._load_memory_from_row(row)
                    if memory:
                        memories.append(memory)

            logger.debug(f"Retrieved {len(memories)} memories from D1")
            return memories

        except Exception as e:
            logger.error(f"Error getting all memories: {str(e)}")
            return []

    async def count_all_memories(self, memory_type: Optional[str] = None) -> int:
        """
        Get total count of memories in storage.

        Args:
            memory_type: Optional filter by memory type

        Returns:
            Total number of memories, optionally filtered by type
        """
        try:
            if memory_type is not None:
                sql = "SELECT COUNT(*) as count FROM memories WHERE memory_type = ?"
                params = [memory_type]
            else:
                sql = "SELECT COUNT(*) as count FROM memories"
                params = []

            payload = {"sql": sql, "params": params}
            response = await self._retry_request("POST", f"{self.d1_url}/query", json=payload)
            result = response.json()

            if not result.get("success"):
                raise ValueError(f"D1 query failed: {result}")

            if result.get("result", [{}])[0].get("results"):
                count = result["result"][0]["results"][0].get("count", 0)
                return int(count)

            return 0

        except Exception as e:
            logger.error(f"Error counting memories: {str(e)}")
            return 0

    async def close(self) -> None:
        """Close the storage backend and cleanup resources."""
        if self.client:
            await self.client.aclose()
            self.client = None

        # Clear embedding cache
        self._embedding_cache.clear()

        logger.info("Cloudflare storage backend closed")