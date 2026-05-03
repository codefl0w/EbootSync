
# fl0w's PS Vita automatic EBOOT pusher daemon
  

A simple UI that extracts the eboot.bin from the selected VPK and pushes it to the selected FTP destination. Defaults to my own config since this was a personal tool. Can be changed in the UI.

## How it works

VPKs execute the eboot.bin, the rest are just assets most of the time. This tool keeps watching the selected VPK path for changes, renames it into a ZIP if it has changed, extracts the eboot.bin, calculates is hash, pushes it to the selected FTP path (which should be the app's data directory, e.g. `/ux0:/app/RANDOMAPP00001/`) and saves the result in a JSON at the script's root. It will keep reading the JSON before each push to make sure no duplicate pushes happen.

IMPORTANT: The script will play a short beep on every successful push. Since it uses winsound, that won't work on Linux or Mac. Edit accordingly.

## Tweaks & recommendations

After you change the values to match your own paths, hit "Save Config" to create a config JSON at the script's root. The script will read it on every boot from that point on.

This tool is recommended to be used with the [**ftpeverywhere**](https://github.com/teakhanirons/ftpeverywhere) plugin installed on the Vita for actual seamless updates.
