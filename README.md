# picokvm-mcp

mcp server to interact with luckfox picokvm.
picokvm is fork of jetkvm, though its JSON/REST API is not compatible somehow.

picokvm has builtin mcp server but it doesn't support storage control and edid setting.

Streamable HTTP is supported in latest firmware though, so maybe it's better to use that for ease

## prerequisites

Python 3.7 or later

## run

```bash
cp .env.example .env
# Edit .env before starting.
python picokvm-mcp.py
```

`picokvm-mcp.py`, `picokvm-upload.py` and `picokvm-download.py` automatically
load `.env` from their own directory. Keep the shared `picokvm_env.py` module alongside them.
Environmental value set in shell takes priority than `.env` values.

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
    - 60 (default)
    - 50
    - 30

- `kvm_reset_edid`: restore the firmware's default EDID

- `kvm_capture_screenshot`: capture the current screen as a JPEG MCP image
  - screenshot retrieval is exposed by internal MCP server instead of usual JSON API, so  
    enable the device MCP server and get an API key in the device settings.
  - you can set PICOKVM_MCP_URL env to use mcp in other host than JSON API.

### status

- `kvm_get_status`: video input and USB attachment status
- `kvm_get_diagnostics`: collect device ID, local app/system versions, network,  
  video, USB, current EDID, current virtual medium and internal shared-storage  
  capacity.

### storage

Use `source: "storage"` (default, `/userdata/picokvm/share`)  
or `source: "sd"` (`/mnt/sdcard`) to select storage. on Base Lite, `storage` is only option.

- `kvm_list_storage_files`: list files at the root of the shared storage, including  
  files shared with the target host over MTP.
- `kvm_delete_storage_file`: delete the file. Unmount the image first if it is in use.
- `kvm_mount_iso`: mount a stored ISO as CDROM.
- `kvm_unmount_iso`: unmount the current virtual medium
- `kvm_get_storage_status`: read the current virtual medium and the capacity of  
  the internal shared-storage filesystem. (no SD support)

To replace a file, call `kvm_delete_storage_file` before uploading same file.

File upload and download is not implemented by this MCP bridge; use `picokvm-upload.py` and `picokvm-download.py`.  
those script supports large file (512MB+) but, SSH SCP transfer is more stable (if its possible) ...

```bash
python picokvm-upload.py archive.zip
```

```bash
python picokvm-download.py archive.zip
```

`picokvm-upload.py` and `picokvm-download.py` uses the same .env as the MCP server.

## note

auth and cors is not implemented by design.

Set `EDID_AUTOSET=1` to prefer 1024x768 @ 60Hz at mcp server startup.

kvm_capture_screenshot saves captured screenshot files on PicoKVM.  
you may need to delete that manually.
