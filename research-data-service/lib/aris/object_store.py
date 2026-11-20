"""XSKY S3 API. Existing bucket only; credentials come from the environment."""
import hashlib
import os
from botocore.config import Config
from botocore.exceptions import ClientError

class ObjectStore:
    def __init__(self, client=None):
        required=['XSKY_ENDPOINT','XSKY_BUCKET','XSKY_ACCESS_KEY','XSKY_SECRET_KEY']
        if client is None:
            missing=[k for k in required if not os.getenv(k)]
            if missing:
                raise ValueError('缺少配置: '+', '.join(missing))
            import boto3
            client=boto3.client('s3', endpoint_url=os.environ['XSKY_ENDPOINT'],
                aws_access_key_id=os.environ['XSKY_ACCESS_KEY'], aws_secret_access_key=os.environ['XSKY_SECRET_KEY'],
                aws_session_token=os.getenv('XSKY_SESSION_TOKEN'), region_name=os.getenv('XSKY_REGION','us-east-1'),
                verify=os.getenv('XSKY_CA_BUNDLE') or True,
                config=Config(proxies={}, connect_timeout=10, read_timeout=60,
                              retries={'max_attempts':3}, s3={'addressing_style':os.getenv('XSKY_ADDRESSING_STYLE','path')}))
        self.client=client
        self.bucket=os.getenv('XSKY_BUCKET','test')
        self.prefix=os.getenv('XSKY_PREFIX','corpus').strip('/')

    def exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if str(e.response.get('Error',{}).get('Code')) in ('404','NoSuchKey','NotFound'):
                return False
            raise

    def key(self, source, image_key, ext):
        digest=hashlib.sha256(image_key.encode()).hexdigest()
        return '/'.join(x for x in (self.prefix, source.split(':',1)[-1], digest[:2],digest+'.'+ext) if x)

    def upload(self, key, body, content_type):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
        if not self.exists(key):
            raise RuntimeError('图片上传后校验失败')
