import { NextRequest } from 'next/server';
export const dynamic = 'force-dynamic';
async function proxy(req: NextRequest) {
  const path = req.nextUrl.pathname + req.nextUrl.search;
  try {
    const headers = new Headers();
    for (const key of ['cookie','content-type','origin']) { const value=req.headers.get(key); if(value) headers.set(key,value); }
    const res = await fetch(`${process.env.BACKEND_URL || 'http://127.0.0.1:8100'}${path}`, {method:req.method,headers,body:req.method==='GET'?undefined:await req.text(),cache:'no-store',signal:AbortSignal.timeout(90000)});
    const out = new Headers({'Cache-Control':'no-store'});
    for(const key of ['content-type','set-cookie','content-disposition']) { const v=res.headers.get(key); if(v)out.set(key,v); }
    return new Response(res.body,{status:res.status,headers:out});
  } catch { return Response.json({detail:'Python service unavailable. Check backend status.'},{status:502}); }
}
export { proxy as GET, proxy as POST, proxy as PUT, proxy as DELETE };
