#!/usr/bin/env bash

_run_setup_rfid_reader() {
    local script="${INSTALLATION_PATH}"/installation/components/setup_rfid_reader.sh
    sudo chmod +x "$script"
    run_and_print_lc "$script"
}

setup_rfid_reader() {
    if [ "$ENABLE_RFID_READER" == true ] ; then
        run_with_log_frame _run_setup_rfid_reader "Install RFID Reader"
    else
        # Hard-coded install, assumes configuration file already exists and
        # SPI dt-overlay is already configured.
        source "${INSTALLATION_PATH}"/.venv/bin/activate
        pip install --upgrade -r "${INSTALLATION_PATH}"/src/jukebox/components/rfid/hardware/rc522_spi/requirements.txt
    fi
}
