from nornir import InitNornir
from nornir.core.task import Task, Result
from nornir.core.filter import F
from nornir_utils.plugins.functions import print_result
from netmiko import Netmiko, ConnectHandler, file_transfer, SCPConn
from datetime import datetime
import logging
import time
import argparse
import os
import re

# globals
current_time = str(datetime.now().strftime('%Y-%m-%d-%H%M-%S'))
config_root = "backup_configs/"
pre_check_root = "pre_checks/"
post_check_root = "post_checks/"
gen_configs_root = "configs/"

# get command line inputs
parser = argparse.ArgumentParser()
parser.add_argument("--network_name", help="Name of network to use.")
parser.add_argument("--upgrade_groups", help="names of router groups, e.g. 8k_routers")
args = parser.parse_args()

# initialize Nornir
nr = InitNornir(config_file=f"inventory/{args.network_name}/config.yaml")
logger = logging.getLogger("nornir.core")


def main():
    # collect all device running OS version
    r = nr.run(
        name="Check software versions on hosts.",
        task=run_check_sw_ver
    )
    logger.info("Router inventory:")
    for host, attributes in nr.inventory.hosts.items():
        logger.info(
            f"\nHost: {host}\n"
            + f"- Hostname: {attributes.name}\n"
            + f"- Current OS version: {r[host][0].result}\n"
            + f"- Target OS version: {nr.inventory.hosts[host]['target_os_ver']}\n"
            + f"- Device group: {attributes.groups}\n"
        )
    hostnames_to_filter = []
    for host, result in r.items():
        target_os_ver = nr.inventory.hosts[host]['target_os_ver']
        # running_os_ver = result[0].result['facts']['os_version']
        running_os_ver = result[0].result
        if target_os_ver not in running_os_ver and running_os_ver != "unknown":
            hostnames_to_filter.append(host)

    # Filter hosts based on the list of hostnames
    filtered_hosts = nr.filter(filter_func=lambda tmp_host: tmp_host.name in hostnames_to_filter)
    filtered_hosts_8k = filtered_hosts.filter(F(groups__contains='8k_routers'))
    filtered_hosts_9k = filtered_hosts.filter(F(groups__contains='9k_routers'))

    if "8k_routers" in args.upgrade_groups:
        logger.info("Will upgrade the following 8k routers:")
        for host, attributes in filtered_hosts_8k.inventory.hosts.items():
            logger.info(
                f"\nHost: {host}\n"
                + f"- Hostname: {attributes.name}\n"
                + f"- Current OS version: {r[host][0].result}\n"
                + f"- Target OS version: {nr.inventory.hosts[host]['target_os_ver']}\n"
                + f"- Device group: {attributes.groups}\n"
            )
        result = filtered_hosts_8k.run( #TODO Check if filtered_hosts is empty
            name="Running upgrade tasks",
            task=upgrade,
            network_name=args.network_name,
        )
        print_result(result)

    if "9k_routers" in args.upgrade_groups:
        logger.info("Will upgrade the following 9k routers:")
        for host, attributes in filtered_hosts_9k.inventory.hosts.items():
            logger.info(
                f"\nHost: {host}\n"
                + f"- Hostname: {attributes.name}\n"
                + f"- Current OS version: {r[host][0].result}\n"
                + f"- Target OS version: {nr.inventory.hosts[host]['target_os_ver']}\n"
                + f"- Device group: {attributes.groups}\n"
            )
        result = filtered_hosts_9k.run(
            name="Running upgrade tasks",
            task=upgrade,
            network_name=args.network_name,
        )
        print_result(result)

    logger.info("Script completed.")
    logger.info("Script stop time is " + current_time)


def upgrade(task: Task, network_name: str) -> Result:
    task.run(
        name="Backing up router configurations.",
        task=backup_configs_ssh,
        network_name=network_name,
    )
    task.run(
        name="Running pre-checks.",
        task=run_checks,
        network_name=args.network_name,
        commands=task.host['pre_check_commands'],
        check_type="pre_check"
    )
    r = task.run(
        name="Copy image file to the router.",
        task=run_copy_file,
    )
    if not r[0].result:
        return Result(
            host=task.host,
            result=f"{task.host} image transfer failed. Exiting upgrade.",
        )
    r = task.run(
        name="Enable fpd auto upgrade.",
        task=configure_CLI,
        commands=["fpd auto-upgrade enable"],
    )
    if not r[0].result:
        return Result(
            host=task.host,
            result=f"{task.host} failed to enable fpd auto upgrade.  Exiting upgrade.",
        )
    for install_command in task.host['install_commands']:
        r = task.run(
            name="Run install image.",
            task=run_install,
            install_command=install_command['command'],
        )
        if not r[0].result:
            return Result(
                host=task.host,
                result=f"{task.host} install failed.",
            )
        if install_command['reload']:
            r = task.run(
                name="Try to reconnect to router after reload.",
                task=reconnect,
            )
            if not r[0].result:
                return Result(
                    host=task.host,
                    result=f"{task.host} failed to reconnect to device after reload.",
                )
            r = task.run(
                host=task.host,
                name="Pause upgrade while device is rebooting.",
                task=pause_upgrade,
            )
    task.run(
        name="Running post-checks.",
        task=run_checks,
        network_name=args.network_name,
        commands=task.host['pre_check_commands'],
        check_type="post_check"
    )
    r = task.run(
        name="Check router running software version.",
        task=run_check_sw_ver,
        # network_name=network_name
    )
    sw_ver = r[0].result
    return Result(
        host=task.host,
        result=f"{task.host} is now running {sw_ver}.",
    )


def configure_CLI(task: Task, commands: list) -> Result:
    try:
        my_connection = ConnectHandler(
            ip=task.host.hostname,
            username=task.host.username,
            password=task.host.password,
            device_type='cisco_xr',
            port=task.host.port,
        )
    except Exception as e:
        logger.error(f"{task.host}: Could not connect to device.  Failed to execute CLI commands: {commands}")
        return Result(
            host=task.host,
            result=False
        )
    my_connection.find_prompt()
    device_response = my_connection.send_config_set(commands)
    logger.info(device_response)
    device_response = my_connection.commit()
    logger.info(device_response)
    if "fail" not in device_response.lower():
        logger.info(f"{task.host}: Successfully applied commands: {commands}")
        result = True
    # else:
    #     logger.error(f"{task.host}: Failed to apply commands: {commands}")
    #     result = False
    my_connection.disconnect()
    return Result(
        host=task.host,
        result=result
    )


def run_checks(task: Task, network_name: str, commands: list, check_type: str) -> Result:
    if check_type == "pre_check":
        check_path = os.path.join(pre_check_root, network_name, current_time)
    elif check_type == "post_check":
        check_path = os.path.join(post_check_root, network_name, current_time)

    try:
        logger.info("============================================================")
        logger.info(f"Copying check data to dir: {check_path}")
        logger.info("============================================================")

        if not os.path.isdir(check_path):
            os.makedirs(check_path, exist_ok=True)
    except Exception as e:
        logger.error((f"{task.host}: Invalid path for pre or post check directory."))
        return Result(
            host=task.host,
            result=False
        )
    try:
        my_connection = ConnectHandler(
            ip=task.host.hostname,
            username=task.host.username,
            password=task.host.password,
            device_type='cisco_xr',
            port=task.host.port,
        )
    except Exception as e:
        logger.info(f"{task.host}: Could not connect to device. Pre-checks failed.")
        return Result(
            host=task.host,
            result=False
        )
    my_connection.find_prompt()
    device_response = my_connection.send_command("term len 0")
    logger.info(device_response)
    command_responses = ""
    for command in commands:
        device_response = my_connection.send_command(command, read_timeout=60)
        device_response = device_response.strip()
        device_response = re.sub('\s*$', "", device_response)
        command_responses += f"********************************* {command} *************************************\n"
        command_responses += device_response + "\n"
    pre_check_file = f"{check_path}/{str(task.host.name)}_pre_checks.txt"
    logger.info(f"DEVICE: {str(task.host.name)} Pre-checks: {pre_check_file}")
    logger.info("============================================================")
    lines = command_responses.splitlines(True)
    with open(pre_check_file, 'a', encoding="utf8") as f:
        f.writelines(lines)
        f.close()
    my_connection.disconnect()
    return Result(
        host=task.host,
        result=True
    )


def run_copy_file(task: Task) -> Result:
    try:
        my_connection = ConnectHandler(
            ip=task.host.hostname,
            username=task.host.username,
            password=task.host.password,
            device_type='cisco_xr',
            port=task.host.port,
        )
    except Exception as e:
        logger.info(f"{task.host}: Could not connect to device. Failed to copy image file.")
        return Result(
            host=task.host,
            result=False
        )
    my_connection.find_prompt()
    copy_command = f"copy http://{task.host['http_server_ip']}/images/{task.host['image_file']} harddisk:"
    device_response = my_connection.send_command_timing(copy_command, read_timeout=60)
    logger.info(device_response)
    if 'Destination filename' in device_response:
        device_response += my_connection.send_command_timing('\r', read_timeout=300)
    logger.info(device_response)
    if "success" in device_response.lower():
        result = True
    else:
        result = False
    my_connection.disconnect()
    return Result(
        host=task.host,
        result=result
    )


def run_check_sw_ver(task: Task) -> Result:
    result = "unknown"
    try:
        my_connection = ConnectHandler(
            ip=task.host.hostname,
            username=task.host.username,
            password=task.host.password,
            device_type='cisco_xr',
            port=task.host.port,
        )
    except Exception as e:
        logger.info(f"{task.host}: Could not connect to device. Failed to check software version.")
        return Result(
            host=task.host,
            result=result
        )
    my_connection.find_prompt()
    copy_command = f"show version"
    device_response = my_connection.send_command_timing(copy_command, read_timeout=60)
    response_lines = device_response.split('\n')
    for line in response_lines:
        if line.startswith(" Version"):
            result = line.split(':')[1]
    my_connection.disconnect()
    return Result(
        host=task.host,
        result=result
    )


def run_install(task: Task, install_command: str) -> Result:
    result = False
    try:
        my_connection = ConnectHandler(
            ip=task.host.hostname,
            username=task.host.username,
            password=task.host.password,
            device_type='cisco_xr',
            port=task.host.port,
        )
    except Exception as e:
        logger.info(f"{task.host}: Could not connect to device. Failed to initiate upgrade installation.")
        return Result(
            host=task.host,
            result=False
        )
    my_connection.find_prompt()
    device_response = my_connection.send_command_timing(install_command, read_timeout=1800)
    logger.info(device_response)
    upgrade_complete = False
    if "Failed" not in device_response:
        # Continuously check for the completion message
        count = 0
        total_response = ""
        total_response += device_response
        while not upgrade_complete:
            # Read the device_response from the connection
            device_response = my_connection.read_channel()
            total_response += device_response
            response_strings = ["completed without error",
                                "install add action completed successfully",
                                "activate action completed successfully",
                                "success",
                                ]
            # Check if the completion message is in the device_response
            if any(response in total_response.lower() for response in response_strings):
                upgrade_complete = True
                result=True
                logger.info(f"{task.host}: Upgrade completed without error.")
            elif "fail" in total_response.lower():
                upgrade_complete = True
                result=False
                logger.error(f"{task.host}: Install failed, check router 'show install request'.")
            elif not my_connection.is_alive():
                upgrade_complete = True
                result=True
                logger.info(f"{task.host}: Device no longer connected, check router 'show install request'.")
            elif count > 1800:
                logger.info(f"{task.host}: Time exceeded, exiting install task.")
                result=False
                break
            else:
                if count % 60 == 0:
                    logger.info(device_response)
                    print(f"{task.host}: Waiting for completion message...")
                time.sleep(1)  # Wait for 6 seconds before checking again
                count += 1
        logger.info(total_response)
    else:
        logger.error(f"{task.host}: Install failed, check router 'show install request'.")
        additional_response = my_connection.read_channel()
        total_response = device_response + additional_response
        logger.info(total_response)
    my_connection.disconnect()
    return Result(
        host=task.host,
        result=result
    )


def reconnect(task: Task) -> Result:
    result = False
    try:
        # Wait for the device to reload
        logger.info(f"{task.host}: Waiting for the device to come back up...")
        time.sleep(120)  # Wait for 2 minutes (adjust as necessary)

        # Try to reconnect
        count = 0
        while True and count <= 25:
            try:
                my_connection = ConnectHandler(
                    ip=task.host.hostname,
                    username=task.host.username,
                    password=task.host.password,
                    device_type='cisco_xr',
                    port=task.host.port,
                )
                my_connection.find_prompt()
                logger.info(f"{task.host}: Reconnected to the device.")
                result = True
                my_connection.disconnect()
                break
            except Exception as e:
                logger.info(f"{task.host}: Device is not yet available. Retrying in 30 seconds...", exc_info=False)
                count += 1
                time.sleep(30)
    except Exception as e:
        logger.info(f"{task.host}: An error occurred: {e}")
    return Result(
        host=task.host,
        result=result
    )


def pause_upgrade(task: Task) -> Result:
    logger.info(f"{task.host}: Pausing upgrade for 2 minutes...")
    time.sleep(120)  # Wait for 2 minutes (adjust as necessary)
    return Result(
        host=task.host,
        result=True
    )


def backup_configs_ssh(task: Task, network_name: str) -> Result:
    topo_path = os.path.join(config_root, network_name, current_time)
    logger.info("============================================================")
    logger.info(f"Copying configurations to dir: {topo_path}")
    logger.info("============================================================")

    if not os.path.isdir(topo_path):
        os.makedirs(topo_path, exist_ok=True)

    my_connection = ConnectHandler(
        ip=task.host.hostname,
        username=task.host.username,
        password=task.host.password,
        device_type='cisco_xr',
        port=task.host.port,
    )
    show_command = "show running"
    my_connection.find_prompt()
    deviceResponse = my_connection.send_command_expect("term len 0")
    deviceResponse = my_connection.send_command(show_command, read_timeout=60)
    deviceResponse = deviceResponse.strip()
    deviceResponse = re.sub('\s*$', "", deviceResponse)
    config_file = topo_path + "/" + str(task.host.name) + ".txt"
    logger.info(f"DEVICE: {str(task.host.name)} Config: {config_file}")
    logger.info("============================================================")
    lines = deviceResponse.splitlines(True)
    with open(config_file, 'a', encoding="utf8") as f:
        f.writelines(lines[2:])
        f.close()
    my_connection.disconnect()


if __name__ == '__main__':
    main()
