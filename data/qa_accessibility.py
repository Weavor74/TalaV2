"""NAME
       qa_accessibility - inspect native control geometry without input
DESCRIPTION
       Reads the foreground window's Windows UI Automation tree.
"""
import comtypes.client
import pyautogui
import tkinter as tk
test = tk.Tk()
test.title('Tala Accessibility QA')
tk.Entry(test).pack()
tk.Button(test,text='Verify').pack()
test.update()
m = comtypes.client.GetModule('UIAutomationCore.dll')
uia = comtypes.client.CreateObject(m.CUIAutomation, interface=m.IUIAutomation)
root = uia.ElementFromHandle(pyautogui.getWindowsWithTitle('Tala Accessibility QA')[0]._hWnd)
walker = uia.ControlViewWalker
def visit(element, depth=0):
    if depth > 5:
        return
    rect = element.CurrentBoundingRectangle
    print(depth, element.CurrentControlType, repr(element.CurrentName), element.CurrentClassName, (rect.left,rect.top,rect.right,rect.bottom), element.CurrentIsKeyboardFocusable, flush=True)
    child = walker.GetFirstChildElement(element)
    while child:
        visit(child, depth+1)
        child = walker.GetNextSiblingElement(child)
visit(root)
test.destroy()
