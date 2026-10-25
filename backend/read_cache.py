"""Short-lived coalescing for dashboard reads; never used for order execution."""
import asyncio,time
class ReadCache:
    def __init__(self,ttl=3):self.ttl=ttl;self.values={};self.pending={};self.generation=0
    def clear(self):self.values.clear();self.pending.clear();self.generation+=1
    async def get(self,key,loader):
        cached=self.values.get(key)
        if cached and time.monotonic()-cached[0]<self.ttl:return cached[1]
        task=self.pending.get(key)
        if task is None:
            generation=self.generation
            async def load():
                try:
                    value=await loader()
                    if self.generation==generation:self.values[key]=(time.monotonic(),value)
                    return value
                finally:
                    if self.pending.get(key) is asyncio.current_task():self.pending.pop(key,None)
            task=asyncio.create_task(load());self.pending[key]=task
        return await asyncio.shield(task)
