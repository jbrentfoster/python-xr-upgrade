from nornir import InitNornir
from nornir.core.task import Task, Result
from nornir_utils.plugins.functions import print_result
from netmiko import Netmiko, ConnectHandler, file_transfer, SCPConn
from datetime import datetime
import logging
import time
import argparse
import os
import re
from nornir_napalm.plugins.tasks import napalm_get

# globals
current_time = str(datetime.now().strftime('%Y-%m-%d-%H%M-%S'))
config_root = "backup_configs/"
gen_configs_root = "configs/"

# get command line inputs
parser = argparse.ArgumentParser()
parser.add_argument("--network_name", help="Name of network to use.")
# parser.add_argument("--image_file_name", help="Name of image file to use for upgrade.")
args = parser.parse_args()

# initialize Nornir
nr = InitNornir(config_file=f"inventory/{args.network_name}/config.yaml")
logger = logging.getLogger("nornir.core")

def main():
    r = nr.run(
        name="get_facts",
        task=get_network,
        network_name=args.network_name
    )

    hostnames_to_filter = []
    for host, result in r.items():
        target_os_ver = nr.inventory.hosts[host]['os_ver']
        # running_os_ver = result[0].result['facts']['os_version']
        running_os_ver = result[0].result
        if target_os_ver != running_os_ver:
            hostnames_to_filter.append(host)

    # Filter hosts based on the list of hostnames
    filtered_hosts = nr.filter(filter_func=lambda host: host.name in hostnames_to_filter)
    logger.info("Will upgrade the following routers:")
    for host, data in filtered_hosts.inventory.hosts.items():
        logger.info(
            f"Host: {host} "
            + f"- Hostname: {data.name}"
            + f"- Current OS version: {r[host][0].result}"
        )
    result = filtered_hosts.run(
        name="Running upgrade tasks",
        task=upgrade_8000,
        network_name=args.network_name,
        image_id=2,
        install_id=2
    )
    print_result(result)

    #todo come up with different way of specifying upgrade, maybe CLI arguments?
    
    logger.info("Script completed successfully")
    logger.info("Script stop time is " + current_time)


def upgrade_8000(task: Task, network_name: str, image_id: int, install_id: int) -> Result:
    task.run(
        name="Backing up router configurations.",
        task=pull_configs_ssh,
        network_name=network_name,
    )
    r = task.run(
        name="Copy image file to the router.",
        task=run_copy_file,
        image_file_id=image_id,
    )
    if not r[0].result:
        return Result(
            host=task.host,
            result=f"{task.host} image transfer failed.",
        )
    r = task.run(
        name="Run install image.",
        task=run_install,
        install_id=install_id,
    )
    if not r[0].result:
        return Result(
            host=task.host,
            result=f"{task.host} install failed.",
        )
    r = task.run(
        name="Try to reconnect to router after reload.",
        task=reconnect,
    )
    if not r[0].result:
        return Result(
            host=task.host,
            result=f"{task.host} failed to reconnect.",
        )
    # r = task.run(
    #     name="Run install commit.",
    #     task=run_install,
    #     install_id=-1
    # )
    # if not r[0].result:
    #     return Result(
    #         host=task.host,
    #         result=f"{task.host} install commit failed.",
    #     )
    # task.run(
    #     name="Pausing upgrade.",
    #     task=pause_upgrade
    # )
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


def get_network(task: Task, network_name: str) -> Result:
    facts = task.run(napalm_get, getters = ["facts"])
    os_ver = facts.result['facts']['os_version']
    return Result(
        host=task.host,
        result=f"{os_ver}"
    )

def run_copy_file(task: Task, image_file_id: int) -> Result:
    my_connection = ConnectHandler(
        ip=task.host.hostname,
        username=task.host.username,
        password=task.host.password,
        device_type='cisco_xr',
        port=task.host.port,
    )
    my_connection.find_prompt()
    copy_command = f"copy http://{task.host['http_server_ip']}/images/{task.host['image_files'][image_file_id]} harddisk:"
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
    my_connection = ConnectHandler(
        ip=task.host.hostname,
        username=task.host.username,
        password=task.host.password,
        device_type='cisco_xr',
        port=task.host.port,
    )
    my_connection.find_prompt()
    copy_command = f"show version"
    device_response = my_connection.send_command_timing(copy_command, read_timeout=60)
    response_lines = device_response.split('\n')
    result= "unknown"
    for line in response_lines:
        if line.startswith(" Version"):
            result = line.split(':')[1]
    my_connection.disconnect()
    return Result(
        host=task.host,
        result=result
    )

def run_install(task: Task, install_id: int) -> Result:
    my_connection = ConnectHandler(
        ip=task.host.hostname,
        username=task.host.username,
        password=task.host.password,
        device_type='cisco_xr',
        port=task.host.port,
    )
    my_connection.find_prompt()
    if install_id != -1:
        install_command = task.host['install_commands'][install_id]
    else:
        install_command = task.host['install_commit_command']
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

            # Check if the completion message is in the device_response
            if "completed without error" in total_response:
                upgrade_complete = True
                logger.info(f"{task.host}: Upgrade completed without error.")
            elif "fail" in total_response.lower():
                upgrade_complete = True
                logger.info(f"{task.host}: Install failed, check router 'show install request'.")
            elif count > 200:
                logger.info(f"{task.host}: Time exceeded, exiting install task.")
                break
            else:
                if count % 10 == 0:
                    logger.info(device_response)
                    print(f"{task.host}: Waiting for completion message...")
                time.sleep(6)  # Wait for 6 seconds before checking again
                count += 1
        logger.info(total_response)
    my_connection.disconnect()
    result = upgrade_complete
    return Result(
        host=task.host,
        result=result
    )


def reconnect(task: Task) -> Result:
    try:
        # Wait for the device to reload
        logger.info(f"{task.host}: Waiting for the device to come back up...")
        time.sleep(120)  # Wait for 2 minutes (adjust as necessary)

        # Try to reconnect
        result = False
        count = 0
        while True and count <= 10:
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
    logger.info(f"{task.host}: Pausing 2 minutes before checking software version...")
    time.sleep(120)  # Wait for 2 minutes (adjust as necessary)
    return Result(
        host=task.host,
        result=True
    )

def pull_configs_ssh(task: Task, network_name: str) -> Result:
    topo_path = os.path.join(config_root, network_name, current_time)
    logger.info("============================================================")
    logger.info(f"Copying configurations to dir: {topo_path}")
    logger.info("============================================================")

    if not os.path.isdir(topo_path):
        # os.mkdir(topo_path)
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
    # setup_logging()
    main()
