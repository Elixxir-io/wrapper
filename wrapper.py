#!/usr/bin/env python3

#///////////////////////////////////////////////////////////////////////////////
#// Copyright © 2020 xx network SEZC                                          //
#//                                                                           //
#// Use of this source code is governed by a license that can be found in the //
#// LICENSE file                                                              //
#///////////////////////////////////////////////////////////////////////////////

# This script wraps the cMix binaries to provide system management

import argparse
import base64
import json
import logging as log
import os
import stat
import subprocess
import shutil
import sys
import threading
import time
import uuid
import boto3
from OpenSSL import crypto
import hashlib


# FUNCTIONS --------------------------------------------------------------------


def upload(src_path, dst_path, s3_bucket, region,
           access_key_id, access_key_secret):
    """
    Uploads file at src_path to dst_path on s3_bucket using
    the provided access_key_id and access_key_secret.

    :param src_path: Path of the local file
    :type src_path: str
    :param dst_path: Path of the destination on S3 bucket
    :type dst_path: str
    :param s3_bucket: Name of S3 bucket
    :type s3_bucket: str
    :param region: Region of S3 bucket
    :type region: str
    :param access_key_id: Access key ID for bucket access
    :type access_key_id: str
    :param access_key_secret: Access key secret for bucket access
    :type access_key_secret: str
    :return: None
    :rtype: None
    """
    try:
        upload_data = open(src_path, 'rb')
        s3 = boto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=access_key_secret,
            region_name=region).resource("s3")
        s3.Bucket(s3_bucket).put_object(Key=dst_path, Body=upload_data.read())
        log.debug("Successfully uploaded to {}/{} from {}".format(s3_bucket,
                                                                  dst_path,
                                                                  src_path))
    except Exception as error:
        log.error("Unable to upload {} to S3: {}".format(src_path, error),
                  exc_info=True)


def download(src_path, dst_path, s3_bucket, region,
             access_key_id, access_key_secret):
    """
    Downloads file at src_path on s3_bucket to dst_path using
    the provided access_key_id and access_key_secret.

    :param src_path: Path of file on S3 bucket
    :type src_path: str
    :param dst_path: Path of local destination
    :type dst_path: str
    :param s3_bucket: Name of S3 bucket
    :type s3_bucket: str
    :param region: Region of S3 bucket
    :type region: str
    :param access_key_id: Access key ID for bucket access
    :type access_key_id: str
    :param access_key_secret: Access key secret for bucket access
    :type access_key_secret: str
    :return: None
    :rtype: None
    """
    try:
        s3 = boto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=access_key_secret,
            region_name=region).resource("s3")
        s3.Bucket(s3_bucket).download_file(src_path, dst_path)
        log.debug("Successfully downloaded to {} from {}/{}".format(dst_path,
                                                                    s3_bucket,
                                                                    src_path))
    except Exception as error:
        log.error("Unable to download {} from S3: {}".format(src_path, error),
                  exc_info=True)


def start_binary(bin_path, log_file, bin_args):
    """
    Starts the binary at the given path with the given args.
    Returns the newly-created subprocess.

    :param bin_path: Path to the binary
    :type bin_path: str
    :param log_file: Path to the binary log file
    :type log_file: str
    :param bin_args: Arguments for the binary
    :type bin_args: list[str]
    :return: Newly-created subprocess
    :rtype: subprocess.Popen
    """
    with open(log_file, "a") as err_out:
        p = subprocess.Popen([bin_path] + bin_args,
                             stdout=subprocess.DEVNULL,
                             stderr=err_out)
        log.info(bin_path + " started at PID " + str(p.pid))
        return p


def terminate_process(p):
    """
    Terminates the given process.

    :param p: Process to terminate
    :type p: subprocess.Popen
    :return: None
    :rtype: None
    """
    if p is not None:
        pid = p.pid
        log.info("Terminating process {}...".format(pid))
        p.terminate()
        log.info("Process {} terminated with exit code {}".
                 format(pid, p.wait()))


def backup_log(src_path, id_path, s3_bucket, region,
               access_key_id, access_key_secret):
    """
    Uploads the log file at the given path to the given S3 destination.
    Clears local log file after a certain size threshold.
    Run in a separate thread.

    :param src_path: Path of the local file
    :type src_path: str
    :param id_path: Path to the node id which is used to get a unique prefix
    :type id_path: str
    :param region: Region of S3 bucket
    :type region: str
    :param s3_bucket: Name of S3 bucket
    :type s3_bucket: str
    :param access_key_id: Access key ID for bucket access
    :type access_key_id: str
    :param access_key_secret: Access key secret for bucket access
    :type access_key_secret: str
    :return: None
    :rtype: None
    """
    megabyte = 1048576  # Size of one megabyte in bytes
    backup_frequency = 10  # Frequency (in seconds) of log backups
    log_index = 0
    log_name = os.path.basename(src_path)

    while True:
        # Sleep for ten seconds
        time.sleep(backup_frequency)
        log_prefix = get_node_id(id_path)
        try:
            # Back up the log file
            upload(src_path, "{}-{}-{}".format(log_prefix, log_index, log_name),
                   s3_bucket, region, access_key_id, access_key_secret)

            # Get the size of the log file in bytes
            log_size = os.path.getsize(src_path)
            log.debug("Current Log Size: {}".format(log_size))

            # Check if the log file is too large
            if log_size > (100 * megabyte):
                log.warning("Log has reached maximum size. Clearing...")
                # Clear out the log file
                open(src_path, 'w').close()
                # Increment the log_index
                log_index += 1
                log.info("Log has been cleared. New Size: {}".format(
                    os.path.getsize(src_path)))

        except Exception as error:
            log.error("Unable to back up log file: {}".format(error),
                      exc_info=True)


# generated_uuid is a static cached UUID used as the node_id
generated_uuid = None
# read_node_id is a static cached node id when we successfully read it
read_node_id = None


def get_node_id(id_path):
    """
    Obtain the ID of the running node.

    :param id_path: the path to the node id
    :type id_path: str
    :return: The node id OR a UUID by host and time if node id file absent
    :rtype: str
    """
    global generated_uuid, read_node_id
    # if we've already read it successfully from the file, return it
    if read_node_id:
        return read_node_id
    # Read it from the file
    try:
        if os.path.exists(id_path):
            with open(id_path, 'r') as idfile:
                new_node_id = json.loads(idfile.read().strip()).get("id", None)
                if new_node_id:
                    read_node_id = new_node_id
                    return new_node_id
    except Exception as error:
        log.warning("Could not open node ID at {}: {}".format(id_path, error))

    # If that fails, then generate, or use the last generated UUID
    if not generated_uuid:
        generated_uuid = str(uuid.uuid1())
        log.warning("Generating random instance ID: {}".format(generated_uuid))
    return generated_uuid


def verify_cmd(inbuf, public_key_path):
    """
    verify_cmd checks the command signature against the network certificate. 

    :param inbuf: the command file buffer
    :type inbuf: file object
    :param public_key_path: The path to the network public key certificate
    :type public_key_path: str
    :return: The json dict for the command, and if the signature worked or not
    :rtype: dict, bool
    """

    with open(public_key_path, 'r') as file:
        command_json = None
        try:
            key = crypto.load_certificate(crypto.FILETYPE_PEM, file.read())
            command = inbuf.readline().strip()
            command_json = json.loads(command)
            sig_json = json.loads(inbuf.readline())
            signature = base64.b64decode(sig_json.get('signature'))
            crypto.verify(key, signature, bytes(command, 'utf-8'), 'sha256')
            return command_json, True
        except Exception as error:
            log.error("Unable to verify command: {}".format(error))
            return command_json, False


def save_cmd(file_path, dest_dir, valid, cmd_time):
    """
    save_cmd saves the command json files to a log directory

    :param file_path: The source path of the command to save
    :type file_path: str
    :param dest_dir: the destination directory
    :type dest_dir: str
    :param valid: is this a valid command file
    :type valid: bool
    :param cmd_time: the timestamp of the command
    :type cmd_time: timestamp
    :return: Nothing
    :rtype: None
    """
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)

    fparts = os.path.basename(file_path).split('.')
    if not valid:
        fparts[0] = "INVALID_{}".format(fparts[0])
    # destdir/command_2849204.json
    dest = "{}/{}_{}.{}".format(dest_dir, fparts[0], int(cmd_time), fparts[1])
    shutil.copyfile(file_path, dest)


def get_args():
    """
    get_args handles argument parsing for the script
    :return arguments in dict format:
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--disableupdates", action="store_true",
                        help="Disable automatic updates",
                        default=False, required=False)
    parser.add_argument("-l", "--logpath", type=str, default="/opt/xxnetwork/logs/xx.log",
                        help="The path to store logs, e.g. /opt/xxnetwork/node-logs/node.log",
                        required=False)
    parser.add_argument("-i", "--idpath", type=str,
                        default="/opt/xxnetwork/logs/IDF.json",
                        help="Node ID path, e.g. /opt/xxnetwork/logs/nodeIDF.json",
                        required=False)
    parser.add_argument("-b", "--binary", type=str,
                        help="Path to the binary",
                        required=True)
    parser.add_argument("-c", "--configdir", type=str, required=False,
                        help="Path to the config dir, e.g., ~/.xxnetwork/",
                        default=os.path.expanduser("~/.xxnetwork"))
    parser.add_argument("-s", "--s3path", type=str, required=True,
                        help="Path to the s3 management directory")
    parser.add_argument("-m", "--s3managementbucket", type=str,
                        help="Path to the s3 management bucket name")
    parser.add_argument("--s3logbucket", type=str, required=True,
                        help="s3 log bucket name")
    parser.add_argument("--s3accesskey", type=str, required=True,
                        help="s3 access key")
    parser.add_argument("--s3secret", type=str, required=True,
                        help="s3 access key secret")
    parser.add_argument("--s3region", type=str, required=True,
                        help="s3 region")
    parser.add_argument("--tmpdir", type=str, required=False,
                        help="directory for temp files", default="/tmp")
    parser.add_argument("--erroutputpath", type=str, required=False,
                        help="Path to recovered error path", default=None)
    parser.add_argument("--configoverride", type=str, required=False,
                        help="Override for config file path", default="")
    return vars(parser.parse_args())


# INITIALIZATION ---------------------------------------------------------------

# Command line arguments
args = get_args()
print(args)

# Configure logger
log.basicConfig(format='[%(levelname)s] %(asctime)s: %(message)s',
                level=log.INFO, datefmt='%d-%b-%y %H:%M:%S')

binary_path = args["binary"]
management_directory = args["s3path"]

# Hardcoded variables
rsa_certificate_path = os.path.expanduser(os.path.join(args["configdir"], "creds",
                                                       "network_management.crt"))
if not os.path.exists(rsa_certificate_path):  # check creds dir for file as well
    rsa_certificate_path = os.path.expanduser(os.path.join(args["configdir"],
                                                           "network_management.crt"))

s3_log_bucket_name = args["s3logbucket"]
s3_management_bucket_name = args["s3managementbucket"]
s3_access_key_id = args["s3accesskey"]
s3_access_key_secret = args["s3secret"]
s3_bucket_region = args["s3region"]
log_path = args["logpath"]
os.makedirs(os.path.dirname(args["logpath"]), exist_ok=True)

err_output_path = args["erroutputpath"]
version_file = management_directory + "/version.jsonl"
command_file = management_directory + "/command.jsonl"
tmp_dir = "/tmp"
remotes_paths = [version_file, command_file]
cmd_log_dir = os.path.expanduser(os.path.join(args["configdir"], "cmdlog"))

# Config file is the binaryname.yaml inside the config directory
config_file = os.path.expanduser(os.path.join(
    args["configdir"], os.path.basename(binary_path) + ".yaml"))
config_override = os.path.abspath(args["configoverride"])

if os.path.isfile(config_override):
    config_file = config_override

# The valid "install" paths we can write to, with their local paths for
# this machine
valid_paths = {
    "binary": os.path.abspath(os.path.expanduser(binary_path)),
    "wrapper": os.path.abspath(sys.argv[0]),
    "config": config_file,
    "cert": rsa_certificate_path
}

# Record the most recent command timestamp
# to avoid executing duplicate commands
timestamps = [0, time.time()]

# Globally keep track of the process being wrapped
process = None

# CONTROL FLOW -----------------------------------------------------------------

# Note this is done before the thread split to guarantee the same uuid.
node_id = get_node_id(args["idpath"])

# Start the log backup service
thr = threading.Thread(target=backup_log,
                       args=(log_path, args["idpath"],
                             s3_log_bucket_name, s3_bucket_region,
                             s3_access_key_id, s3_access_key_secret))
thr.start()

# Frequency (in seconds) of checking for new commands
command_frequency = 10
log.info("Script initialized at {}".format(time.time()))

# Main command/control loop
while True:
    time.sleep(command_frequency)

    # If there is a recovered error file present, restart the server
    if err_output_path and os.path.isfile(err_output_path):
        log.warning("Restarting binary due to error...")
        try:
            if not (process is None or process.poll() is not None):
                process.terminate()

            if os.path.isfile(config_file):
                process = start_binary(binary_path, log_path,
                                       ["--config", config_file])
            else:
                process = start_binary(binary_path, log_path, [])
        except IOError as err:
            log.error(err)

    for i, remote_path in enumerate(remotes_paths):
        try:
            # Obtain the latest management instructions
            local_path = "{}/{}".format(tmp_dir, os.path.basename(remote_path))
            download(remote_path, local_path,
                     s3_management_bucket_name, s3_bucket_region,
                     s3_access_key_id, s3_access_key_secret)

            # Load the management instructions into JSON
            with open(local_path, 'r') as cmd_file:
                signed_commands, ok = verify_cmd(cmd_file, rsa_certificate_path)
                if signed_commands is None:
                    log.error("Empty command file: {}".format(local_path))
                    save_cmd(local_path, cmd_log_dir, False, time.time())
                    continue

                if not ok:
                    log.error("Failed to verify signature for {}!".format(
                        local_path), exc_info=True)
                    save_cmd(local_path, cmd_log_dir, ok, time.time())
                    continue

            timestamp = signed_commands.get("timestamp", 0)
            # Save the command into a log
            save_cmd(local_path, cmd_log_dir, ok, timestamp)

            # If the commands occurred before the script, skip
            # Note: We do not update unless we get a command we
            # have verified and can actually attempt to run.
            if timestamp <= timestamps[i]:
                log.debug("Command set with timestamp {} is outdated, "
                          "ignoring...".format(timestamp))
                continue

            # Note: We get the UUID for every valid command in case it changes
            node_id = get_node_id(args["idpath"])

            # Execute the commands in sequence
            for command in signed_commands.get("commands", list()):
                # If the command does not apply to us, note that and move on
                if "nodes" in command:
                    node_targets = command.get("nodes", list())
                    if node_targets and node_id not in node_targets:
                        log.info("Command does not apply to {}".format(node_id))
                        timestamps[i] = timestamp
                        continue

                command_type = command.get("command", "")
                info = command.get("info", dict())

                log.info("Executing command: {}".format(command))
                if command_type == "start":
                    # If the process is not running, start it
                    if process is None or process.poll() is not None:
                        if os.path.isfile(config_file):
                            process = start_binary(binary_path, log_path,
                                       ["--config", config_file])
                        else:
                            process = start_binary(binary_path, log_path, [])

                elif command_type == "stop":
                    # Stop the wrapped process
                    terminate_process(process)

                elif command_type == "delay":
                    # Delay for the given amount of time
                    # NOTE: Provided in MS, converted to seconds
                    duration = info.get("time", 0)
                    log.info("Delaying for {}ms...".format(duration))
                    time.sleep(duration / 1000)

                elif command_type == "update":
                    if args["disableupdates"]:
                        log.error("Update command ignored, updates disabled!")
                        timestamps[i] = timestamp
                        continue

                    # Verify valid install path
                    install_path = info.get("install_path", "")
                    if install_path not in valid_paths.keys():
                        log.error("Invalid install path: {}. Expected one of: {}".format(
                            install_path, valid_paths.values()))
                        timestamps[i] = timestamp
                        continue
                    install_path = valid_paths[install_path]

                    # Update the local binary with the remote binary
                    update_path = "{}/{}".format(management_directory,
                                                 info.get("path", ""))
                    log.info("Updating file at {} to {}...".format(
                        update_path, install_path))
                    os.makedirs(os.path.dirname(install_path), exist_ok=True)
                    tmp_path = install_path + ".tmp"
                    download(update_path, tmp_path,
                             s3_management_bucket_name, s3_bucket_region,
                             s3_access_key_id, s3_access_key_secret)

                    # If the hash matches, overwrite to binary_path
                    update_bytes = bytes(open(tmp_path, 'rb').read())
                    actual_hash = hashlib.sha256(update_bytes).hexdigest()
                    expected_hash = info.get("sha256sum", "")
                    if actual_hash != expected_hash:
                        log.error("Binary {} does not match hash {}".format(
                            tmp_path, expected_hash))
                        timestamps[i] = timestamp
                        continue

                    # Move the file into place, overwriting anything
                    # that's there.
                    try:
                        os.replace(tmp_path, install_path)
                    except Exception as err:
                        log.error("Could not overwrite {} with {}: {}".format(
                            binary_path, tmp_path, err))
                        timestamps[i] = timestamp
                        continue
                    # Make the binary executable
                    os.chmod(binary_path, stat.S_IEXEC)

                    if install_path == "wrapper":
                        log.info("Wrapper script updated, exiting now...")
                        sys.exit(0)
                log.info("Completed command: {}".format(command))

            # Update the timestamp in order to avoid repetition
            timestamps[i] = timestamp
        except Exception as err:
            log.error("Unable to execute commands: {}".format(err),
                      exc_info=True)
