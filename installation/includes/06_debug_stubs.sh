# Manipulate file descriptor for logging
_setup_logging(){
    if [ "$CI_RUNNING" == "true" ]; then
        exec 3>&1 2>&1
    else
        exec 3>&1 1>>"${INSTALLATION_LOGFILE}" 2>&1 || { echo "ERROR: Cannot create log file."; exit 1; }
    fi
    echo "Log start: ${INSTALL_ID}"
}

# Function to log to both console and logfile
print_lc() {
  local message="$1"
  echo -e "$message"
}

# Function to log to logfile only
log() {
  local message="$1"
  echo -e "$message"
}

# Function to run a command where the output will be logged to both console and logfile
run_and_print_lc() {
  "$@"
}

# Function to log to console only
print_c() {
  local message="$1"
  echo -e "$message" 1>&3
}

# Function to clear console screen
clear_c() {
  clear 1>&3
}

# Generic emergency error handler that exits the script immediately
# Print additional custom message if passed as first argument
# Examples:
#   a command || exit_on_error
#   a command || exit_on_error "Execution of command failed"
exit_on_error () {
  print_lc "\n****************************************"
  print_lc "ERROR OCCURRED!
A non-recoverable error occurred.
Check install log for details:"
  print_lc "$INSTALLATION_LOGFILE"
  print_lc "****************************************"
  if [[ -n $1 ]]; then
    print_lc "$1"
    print_lc "****************************************"
  fi
  log "Abort!"
}