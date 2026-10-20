"""Verify independent local S3 services using real HTTP clients and backend state."""
import os
import unittest

@unittest.skipUnless(os.environ.get("OSV_RUN_MOTO_TESTS")=="1","install the pinned local Moto service first")
class LocalMotoIsolationTest(unittest.TestCase):
    """Prevent test classes from sharing Moto's module-global buckets across server lifetimes."""
    def client(self,server):
        import boto3
        host,port=server.get_host_and_port()
        return boto3.client("s3",endpoint_url=f"http://{host}:{port}",region_name="us-east-1",aws_access_key_id="same-account",aws_secret_access_key="same-secret")

    def test_sequential_services_start_empty(self):
        """Store real bytes in one service, stop it, then start another; expect an empty account and no inherited buckets."""
        from local_moto import LocalMotoServer
        first=LocalMotoServer();first.start()
        try:
            client=self.client(first);client.create_bucket(Bucket="isolation-sequential")
            client.put_object(Bucket="isolation-sequential",Key="sentinel",Body=b"first-service")
            self.assertEqual(client.get_object(Bucket="isolation-sequential",Key="sentinel")["Body"].read(),b"first-service")
        finally:first.stop()
        second=LocalMotoServer();second.start()
        try:self.assertEqual(self.client(second).list_buckets()["Buckets"],[])
        finally:second.stop()

    def test_concurrent_services_keep_same_bucket_and_key_independent(self):
        """Write different bytes under identical bucket/key/account in two live services; expect each actual GET to retain its own data even after the other service stops."""
        from local_moto import LocalMotoServer
        first=LocalMotoServer();first.start();second=LocalMotoServer();second.start()
        try:
            a=self.client(first);b=self.client(second)
            for client,payload in ((a,b"first-bytes"),(b,b"second-bytes")):
                client.create_bucket(Bucket="isolation-concurrent");client.put_object(Bucket="isolation-concurrent",Key="same",Body=payload)
            self.assertEqual(a.get_object(Bucket="isolation-concurrent",Key="same")["Body"].read(),b"first-bytes")
            self.assertEqual(b.get_object(Bucket="isolation-concurrent",Key="same")["Body"].read(),b"second-bytes")
            second.stop()
            self.assertEqual(a.get_object(Bucket="isolation-concurrent",Key="same")["Body"].read(),b"first-bytes")
        finally:second.stop();first.stop()
