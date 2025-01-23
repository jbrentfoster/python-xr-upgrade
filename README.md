# Python XR Upgrade
This python script is intended as an example workflow for upgrading IOS XR routers.  

Before using for production purposes:
* additional pre- and post- checks should be added
* workflow should be customized for your scenario
* extensively test in a lab environment with your hardware and specific software versions, router configurations, etc

## Setup
### Clone the repo
```commandline
git clone https://wwwin-github.cisco.com/brfoster/python-xr-upgrade.git
```
### Add python virtual environment and install Ansible, Napalm
```
sudo apt -y install python3.10-venv
python3 -m venv xrupgrade_venv
source xrupgrade_venv/bin/activate
pip install nornir
pip install nornir-napalm
pip install nornir-utils
```
### Install HTTP Server
```commandline
sudo apt-get -y install apache2
sudo service apache2 start
cd /var/www/html
sudo mkdir images
sudo chmod 777 images
```
### Copy ISO image files the HTTP server directory
Copy iso image files to the directory configured in the previous step.

`/var/www/html/images`

### Update YAML files
Update the following files:
* /inventory/<network-name>/hosts.yaml
* /inventory/<network-name>/config.yaml
* /inventory/<network-name>/groups.yaml

## Enable Netconf on Routers
Make sure netconf is enabled on the routers
```commandline
netconf-yang agent
ssh server netconf vrf default
```
## Run the script
```commandline
TBA
```