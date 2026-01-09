install() {
  . ../includes/PhonieboxInstall.conf
  if [ -f ../includes/PhonieboxInstall.overwrite.conf ]; then
    . ../includes/PhonieboxInstall.overwrites.conf
  fi
  show_slow_hardware_message
  set_raspi_config
  set_ssh_qos
  update_raspi_os
  init_git_repo_from_tardir
  setup_jukebox_core
  setup_mpd
  setup_samba
  setup_jukebox_webapp
  setup_kiosk_mode
  setup_rfid_reader
  optimize_boot_time
  setup_autohotspot
  setup_postinstall
  cleanup
}
