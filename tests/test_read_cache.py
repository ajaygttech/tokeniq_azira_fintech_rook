import asyncio,unittest
from backend.read_cache import ReadCache
class ReadCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_identical_reads_are_coalesced(self):
        cache=ReadCache();calls=0
        async def load():
            nonlocal calls
            calls+=1;await asyncio.sleep(.01);return {'balance':12}
        results=await asyncio.gather(*(cache.get('same',load) for _ in range(20)))
        self.assertEqual(calls,1);self.assertTrue(all(r=={'balance':12} for r in results));await cache.get('same',load);self.assertEqual(calls,1)
    async def test_different_markets_do_not_block_each_other(self):
        cache=ReadCache();entered=set();ready=asyncio.Event()
        async def load(key):
            entered.add(key)
            if len(entered)==2:ready.set()
            await asyncio.wait_for(ready.wait(),1);return key
        self.assertEqual(await asyncio.gather(cache.get('btc',lambda:load('btc')),cache.get('eth',lambda:load('eth'))),['btc','eth'])
    async def test_cancelled_browser_does_not_cancel_shared_load(self):
        cache=ReadCache();started=asyncio.Event();release=asyncio.Event()
        async def load():started.set();await release.wait();return 5
        first=asyncio.create_task(cache.get('account',load));await started.wait();second=asyncio.create_task(cache.get('account',load));await asyncio.sleep(0);first.cancel()
        with self.assertRaises(asyncio.CancelledError):await first
        release.set();self.assertEqual(await second,5)
    async def test_invalidation_does_not_republish_old_account_state(self):
        cache=ReadCache();started=asyncio.Event();release=asyncio.Event()
        async def old():started.set();await release.wait();return 'old'
        pending=asyncio.create_task(cache.get('account',old));await started.wait();cache.clear()
        async def new():return 'new'
        self.assertEqual(await cache.get('account',new),'new');release.set();await pending;self.assertEqual(await cache.get('account',new),'new')
    async def test_failed_read_retries_and_expired_cache_reloads(self):
        cache=ReadCache(ttl=0)
        async def bad():raise ValueError('unavailable')
        with self.assertRaises(ValueError):await cache.get('key',bad)
        calls=0
        async def good():
            nonlocal calls
            calls+=1;return calls
        self.assertEqual(await cache.get('key',good),1);self.assertEqual(await cache.get('key',good),2)
