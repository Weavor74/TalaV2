import ctypes
from ctypes import wintypes
u=ctypes.windll.user32
@ctypes.WINFUNCTYPE(ctypes.c_bool,wintypes.HWND,wintypes.LPARAM)
def visit(h,l):
 p=wintypes.DWORD();u.GetWindowThreadProcessId(h,ctypes.byref(p))
 if p.value==24048:
  b=ctypes.create_unicode_buffer(512);u.GetWindowTextW(h,b,512)
  if b.value:
   print(h,b.value)
   if 'Tala' in b.value:u.ShowWindow(h,9);u.ShowWindow(h,5)
 return True
u.EnumWindows(visit,0)
