import asyncio,json
from sdk.agent.web_browser import browser_navigate,browser_close
async def main():
 try:
  result=await browser_navigate('https://example.com')
  print(json.dumps(result))
 except Exception as exc:
  print(json.dumps({'success':False,'error':str(exc)}))
 finally:
  await browser_close()
asyncio.run(main())
