# Copyright 2014, 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import shlex
import os
from collections import deque
from . import utils
from .shell import shell

log = logging.getLogger("container")


class NoContainerIPException(Exception):

    "Container has no IP"


class ContainerRunException(Exception):

    "Running cmd in container failed"


def ip(name):
    """ Get IP of container

    Arguments:
    name: Name of container

    Returns:
    Ip of container
    """
    ips = shell("sudo lxc-info -n {} -i -H".format(name))
    ips = ips.output().pop().split()
    log.debug("lxc-info found: '{}'".format(ips))
    if ips.code > 0:
        log.exception("error calling lxc-info to get container IP")
        raise NoContainerIPException()
    if len(ips) == 0:
        raise NoContainerIPException()
    log.debug("using {} as the container ip".format(ips[0].decode()))
    return ips[0].decode()


def run(name, cmd, use_ssh=False, use_sudo=False):
    """ Run a command in container

    Arguments:
    name: Name of container
    cmd: Command to run

    Returns:
    Result of Command
    """

    if use_ssh:
        ipaddr = ip(name)
        quoted_cmd = shlex.quote(cmd)
        wrapped_cmd = ("sudo -H -u {3} TERM=xterm256-color ssh -t -q "
                       "-l ubuntu -o \"StrictHostKeyChecking=no\" "
                       "-o \"UserKnownHostsFile=/dev/null\" "
                       "-o \"ControlMaster=auto\" "
                       "-o \"ControlPersist=600\" "
                       "-i {2} "
                       "{0} {1}".format(ipaddr, quoted_cmd,
                                        utils.ssh_privkey(),
                                        utils.install_user()))
    else:
        ipaddr = "-"
        quoted_cmd = cmd
        wrapped_cmd = []
        if use_sudo:
            wrapped_cmd.append("sudo")
        wrapped_cmd.append("lxc-attach -n {container_name} -- "
                           "{cmd}".format(container_name=name,
                                          cmd=cmd))
        wrapped_cmd = " ".join(wrapped_cmd)

    sh = shell(cmd)
    if sh.code > 0:
        raise ContainerRunException("Problem running {0} in container "
                                    "{1}:{2}/{3}".format(quoted_cmd, name, ip),
                                    sh.code, sh.errors())

    return sh.output().pop()


def status(name, cmd, config):
    """ Runs openstack-status inside Container, replacing parent process

    Arguments:
    name: Name of container
    cmd: Command to run
    config: Application configuration
    """
    ipaddr = ip(name)
    cmd = ("sudo -H -u {2} TERM=xterm256-color ssh -t -q "
           "-l ubuntu -o \"StrictHostKeyChecking=no\" "
           "-o \"UserKnownHostsFile=/dev/null\" "
           "-o \"ControlMaster=auto\" "
           "-o \"ControlPersist=600\" "
           "-i {1} "
           "{0} {3}".format(ipaddr, utils.ssh_privkey(),
                            utils.install_user(), cmd))
    log.debug("Running command without waiting "
              "for response.: {}".format(cmd))
    args = deque(shlex.split(cmd))
    os.execlp(args.popleft(), *args)


def cp(name, filepath, dst):
    """ copy file to container

    Arguments:
    name: name of container
    filepath: file to copy to container
    dst: destination of remote path
    """
    ipaddr = ip(name)
    cmd = ("scp -r -q "
           "-o \"StrictHostKeyChecking=no\" "
           "-o \"UserKnownHostsFile=/dev/null\" "
           "-i {identity} "
           "{filepath} "
           "ubuntu@{ip}:{dst} ".format(ip=ipaddr, dst=dst,
                                       identity=utils.ssh_privkey(),
                                       filepath=filepath))
    sh = shell(cmd)
    if sh.code > 0:
        raise Exception("There was a problem copying ({0}) to the "
                        "container ({1}:{2}): {3}".format(
                            filepath, name, ip, sh.output()))


def create(name, userdata):
    """ creates a container from ubuntu-cloud template

    Arguments:
    name: Name of container
    userdata: Cloud-init userdata file

    Returns:
    Status of command
    """
    # NOTE: the -F template arg is a workaround. it flushes the lxc
    # ubuntu template's image cache and forces a re-download. It
    # should be removed after https://github.com/lxc/lxc/issues/381 is
    # resolved.
    flushflag = "-F"
    if os.getenv("USE_LXC_IMAGE_CACHE"):
        log.debug("USE_LXC_IMAGE_CACHE set, so not flushing in lxc-create")
        flushflag = ""
    sh = shell(
        'sudo -E lxc-create -t ubuntu-cloud '
        '-n {name} -- {flushflag} '
        '-u {userdatafilename}'.format(name=name,
                                       flushflag=flushflag,
                                       userdatafilename=userdata))
    if sh.code > 0:
        raise Exception("Unable to create container: "
                        "{0}".format(sh.output()))
    return sh.code


def start(name, lxc_logfile):
    """ starts lxc container

    Arguments:
    name: name of container
    lxc_logfile: Logfile to write lxc output

    Returns:
    Status code of lxc-start
    """
    sh = shell(
        'sudo lxc-start -n {0} -d -o {1}'.format(name,
                                                 lxc_logfile))

    if sh.code > 0:
        raise Exception("Unable to start container: "
                        "{0}".format(sh.output()))
    return sh.code


def stop(name):
    """ stops lxc container

    Arguments:
    name: name of container

    Returns:
    Status code of lxc-stop
    """
    sh = shell('sudo lxc-stop -n {0}'.format(name))

    if sh.code > 0:
        raise Exception("Unable to stop container: "
                        "{0}".format(sh.output()))

    return sh.code


def destroy(name):
    """ destroys lxc container

    Arguments:
    name: name of container

    Returns:
    Status code of lxc-destroy
    """
    sh = shell('sudo lxc-destroy -n {0}'.format(name))

    if sh.code > 0:
        raise Exception("Unable to destroy container: "
                        "{0}".format(sh.output()))

    return sh.code


def wait_checked(name, check_logfile, interval=20):
    """waits for container to be in RUNNING state, checking
    'check_logfile' every 'interval' seconds for error messages.

    Intended to be used with container_start, which uses 'lxc-start
    -d', which returns 0 immediately and does not detect errors.

    returns when the container 'name' is in RUNNING state.
    raises an exception if errors are detected.
    """
    while True:
        sh = shell('sudo lxc-wait -n {} -s RUNNING '
                   '-t {}'.format(name, interval))
        if sh.code == 0:
            return
        log.debug("{} not RUNNING after {} seconds, "
                  "checking '{}' for errors".format(name, interval,
                                                    check_logfile))
        grepout = shell('grep -q ERROR {}'.format(check_logfile))
        if grepout.code == 0:
            raise Exception("Error detected starting container. See {} "
                            "for details.".format(check_logfile))


def wait(name):
    """ waits for the container to be in a RUNNING state

    Arguments:
    name: name of container

    Returns:
    Status code of lxc-wait
    """
    sh = shell(
        'sudo lxc-wait -n {0} -s RUNNING'.format(name))
    return sh.code
