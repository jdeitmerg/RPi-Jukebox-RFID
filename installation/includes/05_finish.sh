#!/usr/bin/env bash

finish() {
local local_hostname=$(hostname)
  print_lc "####################### FINISHED ########################

Installation complete!

${FIN_MESSAGE}

In order to start, you need to reboot your Raspberry Pi.
Your SSH connection will disconnect.

After the reboot, you can access the Web App in your browser at
http://${local_hostname}.local or http://${CURRENT_IP_ADDRESS}
Don't forget to upload files.
"
}
