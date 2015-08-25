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

""" LXC Container utilities """

import subprocess
import logging
import shlex
import os
import lxc

log = logging.getLogger("uoilib.container")


class NoContainerIPException(Exception):

    "Container has no IP"


class ContainerRunException(Exception):

    "Running cmd in container failed"


class Container:
    """ Container class

    :params str name: Name of container
    :params str run_as: User to run additional commands with
    """
    def __init__(self, name, run_as):
        self.container = lxc.Container(name)
        self.run_as = run_as

    def ip(self):
        try:
            ips = self.container.get_ips()
            log.debug("lxc-info found: '{}'".format(ips))
            if len(ips) == 0:
                raise NoContainerIPException()
            log.debug("using {} as the container ip".format(ips[0]))
            return ips[0]
        except subprocess.CalledProcessError:
            log.exception("error calling lxc-info to get container IP")
            raise NoContainerIPException()

    def run(self, cmd, use_ssh=False, output_cb=None):
        """ run command in container

        :param str name: name of container
        :param str cmd: command to run
        """
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        subproc = subprocess.Popen(cmd,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)

        if subproc.returncode == 0:
            return subproc.stdout
        else:
            log.debug("Error with command: "
                      "[Output] '{}'".format(subproc.stdeer.strip()))

            raise ContainerRunException("Problem running {0} in container "
                                        "{1}:{2}".format(cmd, self.container,
                                                         self.ip()),
                                        subproc.returncode)

    def create(self, userdata):
        """ creates a container from ubuntu-cloud template
        """
        # NOTE: the -F template arg is a workaround. it flushes the lxc
        # ubuntu template's image cache and forces a re-download. It
        # should be removed after https://github.com/lxc/lxc/issues/381 is
        # resolved.
        flushflag = "-F"
        if os.getenv("USE_LXC_IMAGE_CACHE"):
            log.debug("USE_LXC_IMAGE_CACHE set, so not flushing in lxc-create")
            flushflag = ""
        ret = self.container.create(template="ubuntu-cloud",
                                    args=(flushflag, "-u", userdata))
        log.debug("Create container ret: {}".format(ret))
        return ret

    def start(self):
        """ starts lxc container

        """
        ret = self.container.start()
        log.debug("Start container: {}".format(ret))
        return ret

    def stop(self):
        """ stops lxc container

        """
        ret = self.container.stop()
        return ret

    def destroy(self):
        """ destroys lxc container
        """
        if self.container.state == "RUNNING":
            self.container.stop()
        self.container.destroy()
