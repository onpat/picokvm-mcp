# picokvm-mcp

mcp server to interact with luckfox picokvm.
picokvm is fork of jetkvm, though its JSON/REST API is not compatible somehow.

picokvm has builtin mcp server but..., it doesn't support Streamable HTTP 
neither usb storage control.

## prerequisites

Python 3.7 or later

## run

python picokvm-mcp.py

## supported actions

### mouse
mouse movement
mouse click(right, left, middle)
mouse wheel

### keyboard
keyboard input
keyboard conbination input

### screen
change screen resolution
screenshot(base64)

### usb storage
list storage files
mount iso
unmount iso

note: upload files isn't supported (mcp standard doesn't have upload)
