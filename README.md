# picokvm-mcp

mcp server to interact with luckfox picokvm.
picokvm is fork of jetkvm, though its JSON/REST API is not compatible somehow.

picokvm has builtin mcp server but..., it doesn't support Streamable HTTP 
neither usb storage control.

## prerequisites

Python 3.7 or later

## run

```bash
export PICOKVM_BASE_URL='http://192.168.1.100'
python picokvm-mcp.py
```

set PICOKVM_PASSWORD if picokvm has password

## supported actions

### mouse

left-up is `(0, 0)`, right-down is `(width - 1, height - 1)`
relative movement can be -2047 to 2047

- `kvm_mouse_move`: mouse movement (absolute or relative)
- `kvm_mouse_click`: mouse click (right, left, middle)
- `kvm_mouse_wheel`: mouse wheel (vertical and horizontal)

### keyboard

key names are: `Enter`, `Escape`, `Backspace`, `Tab`, `Space` ,`F1`-`F12`, `Delete`, `Home`, `End`, `PageUp`, `PageDown`, `Up`, `Down`, `Left`, `Right` ...
`ctrl/control`, `shift`, `alt`, `meta`, `CtrlRight`, `ShiftLeft`, `AltGr`, `0xE0`-`0xE7` and normal keys

- `kvm_send_key`: send single key
- `kvm_send_key_combination`: send key combination up to 6(normal) or 14(with modifier)
- `kvm_type_text`: send text (up to 1000 ASCII characters, US keyboard)

### screen

- `kvm_set_screen_resolution`: change EDID screen info
  - supported resolution
    - 640x480
    - 800x600
    - 1024x768
    - 1280x720
    - 1280x1024
    - 1920x1080
  - supported refresh rate
    - 60
    - 50
    - 30

todo: add screenshot with base64

### status

- `kvm_get_status`: video input and USB attachment status

### storage

specify sd or internal storage(default, `/userdata/picokvm/share`) by `source` parameter. 

- `kvm_list_storage_files`: list filename, size, created time of files.
- `kvm_mount_iso`: mount a stored ISO as CDROM.
- `kvm_unmount_iso`: unmount the current virtual medium

note: upload files isn't supported (mcp standard doesn't have upload)

## note

auth and cors is not implemented by design

EDID is set on start to prefer 1024x768 @ 60Hz for compatibility.
Set `EDID_AUTOSET` env 0 to disable this function.

screenshot api is not exposed externally than picokvm, so... will implement later
