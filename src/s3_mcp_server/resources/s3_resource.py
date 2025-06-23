import logging
import os
from typing import List, Dict, Any, Optional
import aioboto3
import asyncio
from botocore.config import Config
import base64
import json

logger = logging.getLogger("s3_mcp_server")

class S3Resource:
    """
    S3 Resource provider that handles interactions with AWS S3 buckets.
    Part of a collection of resource providers (S3, DynamoDB, etc.) for the MCP server.
    """

    def __init__(self, region_name: str = None, profile_name: str = None, max_buckets: int = 5):
        """
              Initialize S3 resource provider
              Args:
                  region_name: AWS region name
                  profile_name: AWS profile name
                  max_buckets: Maximum number of buckets to process (default: 5)
              """
        # Configure boto3 with retries and timeouts
        self.config = Config(
            retries=dict(
                max_attempts=3,
                mode='adaptive'
            ),
            connect_timeout=5,
            read_timeout=60,
            max_pool_connections=50
        )

        self.session = aioboto3.Session()
        self.region_name = region_name
        self.max_buckets = max_buckets
        self.configured_buckets = self._get_configured_buckets()

    def _get_configured_buckets(self) -> List[str]:
        """
        Get configured bucket names from environment variables.
        Format in .env file:
        S3_BUCKETS=bucket1,bucket2,bucket3
        or
        S3_BUCKET_1=bucket1
        S3_BUCKET_2=bucket2
        see env.example ############
        """
        # Try comma-separated list first
        bucket_list = os.getenv('S3_BUCKETS')
        if bucket_list:
            return [b.strip() for b in bucket_list.split(',')]

        buckets = []
        i = 1
        while True:
            bucket = os.getenv(f'S3_BUCKET_{i}')
            if not bucket:
                break
            buckets.append(bucket.strip())
            i += 1

        return buckets

    async def list_buckets(self, start_after: Optional[str] = None) -> List[dict]:
        """
        List S3 buckets using async client with pagination
        """
        async with self.session.client('s3', region_name=self.region_name) as s3:
            if self.configured_buckets:
                # If buckets are configured, only return those
                response = await s3.list_buckets()
                all_buckets = response.get('Buckets', [])
                configured_bucket_list = [
                    bucket for bucket in all_buckets
                    if bucket['Name'] in self.configured_buckets
                ]

                #
                if start_after:
                    configured_bucket_list = [
                        b for b in configured_bucket_list
                        if b['Name'] > start_after
                    ]

                return configured_bucket_list[:self.max_buckets]
            else:
                # Default behavior if no buckets configured
                response = await s3.list_buckets()
                buckets = response.get('Buckets', [])

                if start_after:
                    buckets = [b for b in buckets if b['Name'] > start_after]

                return buckets[:self.max_buckets]

    async def create_bucket(self, bucket_name: str, region: Optional[str] = None) -> Dict[str, Any]:
        """
        Create a new S3 bucket
        Args:
            bucket_name: Name of the bucket to create
            region: AWS region for the bucket (defaults to configured region)
        Returns:
            Dict containing bucket creation response
        """
        target_region = region or self.region_name or 'us-east-1'
        
        async with self.session.client('s3', region_name=target_region, config=self.config) as s3:
            try:
                # Check if bucket already exists
                try:
                    await s3.head_bucket(Bucket=bucket_name)
                    logger.warning(f"Bucket {bucket_name} already exists")
                    return {
                        'status': 'exists',
                        'bucket_name': bucket_name,
                        'message': f'Bucket {bucket_name} already exists'
                    }
                except Exception:
                    # Bucket doesn't exist, proceed with creation
                    pass

                # Create bucket configuration
                create_bucket_config = {}
                if target_region != 'us-east-1':
                    create_bucket_config = {
                        'LocationConstraint': target_region
                    }

                # Create bucket
                if create_bucket_config:
                    response = await s3.create_bucket(
                        Bucket=bucket_name,
                        CreateBucketConfiguration=create_bucket_config
                    )
                else:
                    response = await s3.create_bucket(Bucket=bucket_name)

                logger.info(f"Successfully created bucket: {bucket_name}")
                
                # Add to configured buckets if not already there
                if self.configured_buckets and bucket_name not in self.configured_buckets:
                    self.configured_buckets.append(bucket_name)

                return {
                    'status': 'created',
                    'bucket_name': bucket_name,
                    'location': response.get('Location', ''),
                    'region': target_region,
                    'message': f'Bucket {bucket_name} created successfully'
                }

            except Exception as e:
                logger.error(f"Error creating bucket {bucket_name}: {str(e)}")
                raise Exception(f"Failed to create bucket {bucket_name}: {str(e)}")

    async def put_object(self, bucket_name: str, key: str, content: str, 
                        content_type: Optional[str] = None, 
                        is_base64: bool = False,
                        metadata: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Upload an object to S3 bucket
        Args:
            bucket_name: Name of the S3 bucket
            key: Object key (file path in bucket)
            content: Content to upload (string or base64 encoded)
            content_type: MIME type of the content
            is_base64: Whether content is base64 encoded
            metadata: Optional metadata dictionary
        Returns:
            Dict containing upload response information
        """
        if self.configured_buckets and bucket_name not in self.configured_buckets:
            raise ValueError(f"Bucket {bucket_name} not in configured bucket list")

        async with self.session.client('s3', region_name=self.region_name, config=self.config) as s3:
            try:
                # Prepare content
                if is_base64:
                    body = base64.b64decode(content)
                else:
                    body = content.encode('utf-8') if isinstance(content, str) else content

                # Determine content type if not provided
                if not content_type:
                    content_type = self._get_content_type(key)

                # Prepare put_object parameters
                put_params = {
                    'Bucket': bucket_name,
                    'Key': key,
                    'Body': body,
                    'ContentType': content_type
                }

                # Add metadata if provided
                if metadata:
                    put_params['Metadata'] = metadata

                # Upload object
                response = await s3.put_object(**put_params)

                logger.info(f"Successfully uploaded object: s3://{bucket_name}/{key}")

                return {
                    'status': 'uploaded',
                    'bucket_name': bucket_name,
                    'key': key,
                    'etag': response.get('ETag', '').strip('"'),
                    'content_type': content_type,
                    'size': len(body),
                    'uri': f's3://{bucket_name}/{key}',
                    'message': f'Object uploaded successfully to s3://{bucket_name}/{key}'
                }

            except Exception as e:
                logger.error(f"Error uploading object {key} to bucket {bucket_name}: {str(e)}")
                raise Exception(f"Failed to upload object to s3://{bucket_name}/{key}: {str(e)}")

    def _get_content_type(self, key: str) -> str:
        """
        Determine content type based on file extension
        """
        extension_map = {
            '.txt': 'text/plain',
            '.json': 'application/json',
            '.xml': 'application/xml',
            '.html': 'text/html',
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.pdf': 'application/pdf',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.svg': 'image/svg+xml',
            '.csv': 'text/csv',
            '.md': 'text/markdown',
            '.yml': 'application/x-yaml',
            '.yaml': 'application/x-yaml',
            '.py': 'text/x-python',
            '.zip': 'application/zip',
            '.tar': 'application/x-tar',
            '.gz': 'application/gzip'
        }
        
        # Get file extension
        ext = os.path.splitext(key.lower())[1]
        return extension_map.get(ext, 'application/octet-stream')

    async def list_objects(self, bucket_name: str, prefix: str = "", max_keys: int = 1000) -> List[dict]:
        """
        List objects in a specific bucket using async client with pagination
        Args:
            bucket_name: Name of the S3 bucket
            prefix: Object prefix for filtering
            max_keys: Maximum number of keys to return
        """
        #
        if self.configured_buckets and bucket_name not in self.configured_buckets:
            logger.warning(f"Bucket {bucket_name} not in configured bucket list")
            return []

        async with self.session.client('s3', region_name=self.region_name) as s3:
            response = await s3.list_objects_v2(
                Bucket=bucket_name,
                Prefix=prefix,
                MaxKeys=max_keys
            )
            return response.get('Contents', [])

    async def get_object(self, bucket_name: str, key: str, max_retries: int = 3) -> Dict[str, Any]:
        """
        Get object from S3 using streaming to handle large files and PDFs reliably.
        The method reads the stream in chunks and concatenates them before returning.
        """
        if self.configured_buckets and bucket_name not in self.configured_buckets:
            raise ValueError(f"Bucket {bucket_name} not in configured bucket list")

        attempt = 0
        last_exception = None
        chunk_size = 69 * 1024  # Using same chunk size as example for proven performance

        while attempt < max_retries:
            try:
                async with self.session.client('s3',
                                               region_name=self.region_name,
                                               config=self.config) as s3:

                    # Get the object and its stream
                    response = await s3.get_object(Bucket=bucket_name, Key=key)
                    stream = response['Body']

                    # Read the entire stream in chunks
                    chunks = []
                    async for chunk in stream:
                        chunks.append(chunk)

                    # Replace the stream with the complete data
                    response['Body'] = b''.join(chunks)
                    return response

            except Exception as e:
                last_exception = e
                if 'NoSuchKey' in str(e):
                    raise

                attempt += 1
                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(f"Attempt {attempt} failed, retrying in {wait_time} seconds: {str(e)}")
                    await asyncio.sleep(wait_time)
                continue

        raise last_exception or Exception("Failed to get object after all retries")

    def is_text_file(self, key: str) -> bool:
        """Determine if a file is text-based by its extension"""
        text_extensions = {
            '.txt', '.log', '.json', '.xml', '.yml', '.yaml', '.md',
            '.csv', '.ini', '.conf', '.py', '.js', '.html', '.css',
            '.sh', '.bash', '.cfg', '.properties'
        }
        return any(key.lower().endswith(ext) for ext in text_extensions)