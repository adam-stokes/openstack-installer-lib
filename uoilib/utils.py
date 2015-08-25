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

from subprocess import (Popen, PIPE, call, check_output, STDOUT,
                        check_call, DEVNULL, CalledProcessError)

from jinja2 import Environment, FileSystemLoader
from tornado.util import import_object
import configparser
import errno
import itertools
import json
import logging
import os
import pkgutil
import random
import re
import requests
import shutil
import string
import sys
import time
import yaml

log = logging.getLogger('uoilib.utils')


class UtilsException(Exception):
    pass


def cleanup():
    pid = os.path.join('/var/run/openstack.pid')
    if os.path.isfile(pid):
        os.remove(pid)
    log.debug('Attempting to reset the terminal')
    sys.stderr.write("\x1b[2J\x1b[H")
    call(['stty', 'sane'])
    return


def write_status_file(status='', msg=''):
    """ Writes out a status file

    :param str status: success or fail
    :param str msg: any error/success output
    """
    status_file = os.path.join(install_home(), '.cloud-install/finished.json')
    spew(status_file, json.dumps(dict(status=status, msg=msg)))


def load_ext_charms(plug_path, charm_modules):
    """ Load external charms from plugin directory

    :param plug_path: Top level dir housing 'charms/'
    :param charm_modules: Existing charms usually import by load_charms()
    """
    if not os.path.exists(plug_path):
        raise Exception(
            "Non-existent plugin path '{}' specified.".format(plug_path))
    try:
        sys.path.insert(0, plug_path)
        import charms
    except ImportError as e:
        raise Exception("Problem importing external charms: {}".format(e))

    for (_, mname, _) in pkgutil.iter_modules(charms.__path__):
        charm = import_object('charms.' + mname)

        # Override any system charms
        idx = [idx for idx, i in
               enumerate(charm_modules) if
               i.__charm_class__.name() == charm.__charm_class__.name()]
        if idx:
            charm_modules[idx[0]] = charm
        else:
            charm_modules.extend([import_object('charms.' + mname)])
    log.debug("Found additional charms: {}".format(charm_modules))

    return charm_modules


def load_charms(ext_charm_path=None):
    """ Load known charm modules
    """
    import cloudinstall.charms

    charm_modules = [import_object('cloudinstall.charms.' + mname)
                     for (_, mname, _) in
                     pkgutil.iter_modules(cloudinstall.charms.__path__)]

    if ext_charm_path:
        charm_modules = load_ext_charms(ext_charm_path, charm_modules)

    release_path = os.path.join(install_home(),
                                '.cloud-install/openstack_release')
    if os.path.exists(release_path):
        openstack_release = slurp(release_path)
    else:
        openstack_release = cloudinstall.charms.CharmBase.openstack_release_min

    charm_modules = [m for m in charm_modules if
                     (m.__charm_class__.openstack_release_min <=
                      openstack_release[0].lower())]
    return charm_modules


def load_charm_byname(name):
    """ Load a charm by name

    :param str name: name of charm
    """
    return import_object('cloudinstall.charms.{}'.format(name))


def render_charm_config(dst, **kwargs):
    """ Render a config for setting charm config options

    If a custom charm config is passed on the cli it will
    attempt to merge those additional settings without losing
    any pre-existing charm options.

    :params str dst: Destination save path for charmconf.yaml
    """
    charm_conf = load_template('charmconf.yaml')
    if [
            'install_type',
            'password',
            'release',
            'series',
            'openstack_origin'
    ] not in kwargs:
        raise Exception("Required install_type or "
                        "openstack password not found.")

    template_args = dict(
        install_type=kwargs['type'],
        openstack_password=kwargs['password'])

    if kwargs.get('openstack_tip', False):
        template_args['tip'] = kwargs['openstack_tip']
    template_args['openstack_release'] = kwargs['release']

    ubuntu_series = kwargs['series']
    openstack_release = kwargs['release']
    openstack_origin = ("cloud:{}-{}".format(ubuntu_series,
                                             openstack_release))

    template_args['openstack_origin'] = openstack_origin

    if kwargs['install_type'] == 'Single':
        template_args['worker_multiplier'] = '1'

    # add http proxy settings - should not be necessary as juju sets
    # these in the charm execution environment, but required for
    # openstack-origin-git. See: https://launchpad.net/bugs/1472357

    http_proxy = kwargs.get('http_proxy', '')
    https_proxy = kwargs.get('https_proxy', '')
    template_args['http_proxy'] = http_proxy
    template_args['https_proxy'] = https_proxy

    charm_conf_modified = charm_conf.render(**template_args)
    # dest_yaml_path = os.path.join(config.get('settings', 'cfg_path'),
    #                               'charmconf.yaml')
    spew(dst, charm_conf_modified)


def chown(path, user, group=None, recursive=False):
    """
    Change user/group ownership of file

    :param path: path of file or directory
    :param str user: new owner username
    :param str group: new owner group name
    :param bool recursive: set files/dirs recursively

    """
    try:
        if not recursive or os.path.isfile(path):
            shutil.chown(path, user, group)
        else:
            for root, dirs, files in os.walk(path):
                shutil.chown(root, user, group)
                for item in dirs:
                    shutil.chown(os.path.join(root, item), user, group)
                for item in files:
                    shutil.chown(os.path.join(root, item), user, group)
    except OSError as e:
        raise UtilsException(e)


def ensure_locale():
    """
    Makes sure LC_ALL is defined to something sensible
    """
    locale_conf = slurp('/etc/default/locale')
    for line in locale_conf.split('\n'):
        if line.startswith('#'):
            continue
        if "LC_ALL" in line:
            return
    new_locale = "LC_ALL=\"{}\"".format(os.getenv('LANG', 'C'))
    with open('/etc/default/locale', 'a+') as f:
        f.write(new_locale)
    return


def apt_install(pkgs):
    """ runs apt-get install against space separated list of `pkgs`
    """
    ensure_locale()
    cmd = ("DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -qyf "
           "-o Dpkg::Options::=--force-confdef "
           "-o Dpkg::Options::=--force-confold "
           "install {0}".format(pkgs))
    try:
        ret = check_call(cmd, stdout=DEVNULL, stderr=DEVNULL, shell=True)
        log.debug(ret)
    except CalledProcessError as e:
        log.error("Problem with package install: {0}".format(e))
        pass


def get_command_output(command, timeout=None, user_sudo=False):
    """ Execute command through system shell

    :param command: command to run
    :param timeout: (optional) use 'timeout' to limit time. default 300
    :param user_sudo: (optional) sudo into install users env. default False.
    :type command: str
    :returns: {status: returncode, output: stdout, err: stderr}
    :rtype: dict

    .. code::

        # Get output of juju status
        cmd_dict = utils.get_command_output('juju status')
    """
    cmd_env = os.environ.copy()
    # set consistent locale
    cmd_env['LC_ALL'] = 'C'
    if timeout:
        command = "timeout %ds %s" % (timeout, command)

    if user_sudo:
        command = "sudo -E -H -u {0} {1}".format(install_user(), command)

    try:
        p = Popen(command, shell=True,
                  stdout=PIPE, stderr=PIPE,
                  bufsize=-1, env=cmd_env, close_fds=True)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return dict(ret=127, output="", err="")
        else:
            raise e
    stdout, stderr = p.communicate()
    if p.returncode == 126 or p.returncode == 127:
        stdout = bytes()
    if not stderr:
        stderr = bytes()
    return dict(status=p.returncode,
                output=stdout.decode('utf-8'),
                err=stderr.decode('utf-8'))


def poll_until_true(cmd, predicate, frequency, timeout=600,
                    ignore_exceptions=False):
    """run get_command_output(cmd) every frequency seconds, until
    predicate(output) returns True. Timeout after timeout seconds.

    returns True if call eventually succeeded, or False if timeout was
    reached.

    Exceptions raised during get_command_output are handled as per
    ignore_exceptions. If True, they are just logged. If False, they
    are re-raised.

    """
    start_time = time.time()
    frequency_stub = time.time()
    while True:
        # continue if frequency not met
        if time.time() - frequency_stub <= frequency:
            continue
        try:
            output = get_command_output(cmd)
        except Exception as e:
            if not ignore_exceptions:
                raise e
            else:
                log.debug("**Ignoring** exception: {}".format(e))
        if predicate(output):
            return True
        if time.time() - start_time >= timeout:
            return False


# API
def remote_cp(machine_id, src, dst, juju_home):
    log.debug("Remote copying {src} to {dst} on machine {m}".format(
        src=src,
        dst=dst,
        m=machine_id))
    ret = get_command_output(
        "{juju_home} juju scp {src} {m}:{dst}".format(
            juju_home=juju_home, src=src, dst=dst, m=machine_id))
    log.debug("Remote copy result: {r}".format(r=ret))


# API
def remote_run(machine_id, cmds, juju_home):
    if type(cmds) is list:
        cmds = " && ".join(cmds)
    log.debug("Remote running ({cmds}) on machine {m}".format(
        m=machine_id, cmds=cmds))
    ret = get_command_output(
        "{juju_home} juju run "
        "--machine {m} '{cmds}'".format(juju_home=juju_home,
                                        m=machine_id,
                                        cmds=cmds))
    log.debug("Remote run result: {r}".format(r=ret))
    return ret


def get_host_mem():
    """ Get host memory

    Mostly used as a backup if no data can be pulled from
    the normal means in Machine()
    """
    cmd = get_command_output('head -n1 /proc/meminfo')
    out = cmd['output'].rstrip()
    regex = re.compile('^MemTotal:\s+(\d+)\skB')
    match = re.match(regex, out)
    if match:
        mem = match.group(1)
        mem = int(mem) / 1024 / 1024 + 1
        return int(mem)
    else:
        return 0


def get_host_storage():
    """ Get host storage

    LXC doesn't report storage so we pull from host
    """
    cmd = get_command_output('df -B G --total -l --output=avail'
                             ' -x devtmpfs -x tmpfs | tail -n 1'
                             ' | tr -d "G"')
    if not cmd['status']:
        return cmd['output'].lstrip()
    else:
        return 0


def get_host_cpu_cores():
    """ Get host cpu-cores

    A backup if no data can be pulled from
    Machine()
    """
    cmd = get_command_output('nproc')
    if cmd['output']:
        return cmd['output'].strip()
    else:
        return 'N/A'


def partition(pred, iterable):
    """ Returns tuple of allocated and unallocated systems

    :param pred: status predicate
    :type pred: function
    :param iterable: machine data
    :type iterable: list
    :returns: ([allocated], [unallocated])
    :rtype: tuple

    .. code::

        def is_allocated(d):
            allocated_states = ['started', 'pending', 'down']
            return 'charms' in d or d['agent_state'] in allocated_states
        allocated, unallocated = utils.partition(is_allocated,
                                                 [{state: 'pending'}])
    """
    yes, no = [], []
    for i in iterable:
        (yes if pred(i) else no).append(i)
    return (yes, no)


def randomString(size=6, chars=string.ascii_uppercase + string.digits):
    """ Generate a random string

    :param size: number of string characters
    :type size: int
    :param chars: range of characters (optional)
    :type chars: str

    :returns: a random string
    :rtype: str
    """
    return ''.join(random.choice(chars) for x in range(size))


def random_password(size=32):
    """ Generate a password

    :param int size: length of password
    """
    out = get_command_output("pwgen -s {}".format(size))
    return out['output'].strip()


def time_string():
    """ Time helper

    :returns: formatted current time string
    :rtype: str
    """
    return time.strftime('%Y-%m-%d %H:%M')


def load_template(name, path=None):
    """ load template file

    :param str name: name of template file
    :param str path: alternate location of template location
    """
    if path is None:
        path = '/usr/share/openstack/templates'
    env = Environment(
        loader=FileSystemLoader(path))
    return env.get_template(name)


def install_user():
    """ returns sudo user
    """
    user = os.getenv('SUDO_USER', None)
    if not user:
        user = os.getenv('USER', 'root')
    return user


def install_home():
    """ returns installer user home
    """
    return os.path.expanduser("~" + install_user())


def ssh_read_pubkey():
    """ reads ssh public key
    """
    with open(ssh_pubkey(), 'r') as f:
        return f.read()


def ssh_read_privkey():
    """ reads ssh private key
    """
    with open(ssh_privkey(), 'r') as f:
        return f.read()


def ssh_genkey():
    """ Generates sshkey
    """
    if not os.path.exists(ssh_privkey()):
        user_sshkey_path = os.path.join(install_home(),
                                        '.ssh/id_rsa')
        cmd = "ssh-keygen -N '' -f {0}".format(user_sshkey_path)
        out = get_command_output(cmd, user_sudo=True)
        if out['status'] != 0:
            raise Exception(
                "Unable to generate key: {0}".format(out['output']))
        get_command_output('sudo chown -R {0} {1}'.format(
            install_user(),
            os.path.join(install_home(), '.ssh')))
        get_command_output('chmod 0644 {0}.pub'.format(user_sshkey_path),
                           user_sudo=True)
    else:
        log.debug('ssh keys exist for this user, they will be used instead.')


def read_ini_no_sections(path):
    """ Reads a basic INI like file without sections headers.
    Prepends a default section header for querying.
    """
    ini = open(path)
    config = configparser.ConfigParser()
    config.read_file(itertools.chain(['[DEFAULT]'], ini))
    return config


def read_ini(path):
    """ Reads a basic INI like file.
    """
    if not os.path.isfile(path):
        return False
    config = configparser.ConfigParser()
    config.read(path)
    return config


def write_ini(config):
    path = os.path.join(install_home(), '.cloud-install/config.conf')
    with open(path, 'w') as config_w:
        config.write(config_w)


def read_ini_existing():
    """ Reads ini from existing config file
    """
    path = os.path.join(install_home(), '.cloud-install/config.conf')
    return read_ini(path)


def ssh_pubkey():
    """ returns path of ssh public key
    """
    return os.path.join(install_home(), '.ssh/id_rsa.pub')


def ssh_privkey():
    """ returns path of private key
    """
    return os.path.join(install_home(), '.ssh/id_rsa')


def spew(path, data, owner=None):
    """ Writes data to path

    :param str path: path of file to write to
    :param str data: contents to write
    :param str owner: optional owner of file
    """
    with open(path, 'w') as f:
        f.write(data)
    if owner:
        try:
            chown(path, owner)
        except:
            raise UtilsException("Unable to set ownership of {}".format(path))


def slurp(path):
    """ Reads data from path

    :param str path: path of file
    """
    try:
        with open(path) as f:
            return f.read().strip()
    except IOError:
        raise IOError


def human_to_mb(s):
    """Translates human-readable strings like '10G' to numeric
    megabytes"""

    if len(s) == 0:
        raise Exception("unexpected empty string")

    md = dict(M=1, G=1024, T=1024 * 1024, P=1024 * 1024 * 1024)
    suffix = s[-1]
    if suffix.isalpha():
        return float(s[:-1]) * md[suffix]
    else:
        return float(s)


def mb_to_human(num):
    """Translates float number of bytes into human readable strings."""
    suffixes = ['M', 'G', 'T', 'P']
    if num == 0:
        return '0 B'

    i = 0
    while num >= 1024 and i < len(suffixes) - 1:
        num /= 1024
        i += 1
    return "{:.2f} {}".format(num, suffixes[i])


def format_constraint(k, v):
    vs = str(v)
    if vs.isdecimal():
        vs = mb_to_human(v)
    return "{}={}".format(k, vs)


def macgen():
    """ generates mac addresses
    """
    mac = [0x00, 0x16, 0x3e,
           random.randint(0x00, 0x7f),
           random.randint(0x00, 0xff),
           random.randint(0x00, 0xff)]
    return ':'.join(map(lambda x: "%02x" % x, mac))


def download_url(url, output_file):
    """ Downloads contents from a URL
    :param str url: HTTP resource
    :param str output_file: path to store downloaded contents
    """
    res = requests.get(url)
    if res.ok:
        spew(output_file, res.content.decode('utf-8'))
    else:
        raise UtilsException("Exception downloading {}:{}".format(
            url, res.content))


def update_environments_yaml(config, key, val, provider='local'):
    """ updates environments.yaml base file """
    env_path = config.get('settings.juju', 'environments_yaml')
    if os.path.exists(env_path):
        with open(env_path) as f:
            _env_yaml_raw = f.read()
            env_yaml = yaml.load(_env_yaml_raw)
    else:
        raise UtilsException(
            "{} unavailable, is juju bootstrapped?".format(
                env_path))
    if key in env_yaml['environments'][provider]:
        env_yaml['environments'][provider][key] = val
    with open(env_path, 'w') as f:
        _env_yaml_raw = yaml.safe_dump(env_yaml, default_flow_style=False)
        f.write(_env_yaml_raw)


def juju_env(config):
    """ parses current juju environment """
    env_file = None
    settings = config.get('settings')
    juju_path = config.get('settings.juju', 'path')
    if "Single" in settings['install_type']:
        env_file = 'local.jenv'

    if "Multi" in settings['install_type'] or \
       "Landscape" in settings['install_type']:
        env_file = 'maas.jenv'

    if env_file:
        env_path = os.path.join(juju_path, 'environments', env_file)
    else:
        raise UtilsException('Unable to determine installer type.')

    log.debug("Querying juju env in {}".format(env_path))
    if os.path.exists(env_path):
        with open(env_path) as f:
            return yaml.load(f.read().strip())

    raise UtilsException('Unable to load environments file. Is '
                         'juju bootstrapped?')


def ensure_nested_kvm(name):
    """kvm_intel module defaults to nested OFF. If qemu_system_x86 is not
    installed, this may stay off. Our package installs a
    modprobe.d/openstack.conf file to fix this after reboots, but
    we also try to reload the module to work now.
    """

    if 0 == call("lsmod | grep kvm_amd", shell=True):
        return              # we're fine, kvm_amd has nested on by default
    if 0 != call("lsmod | grep kvm_intel", shell=True):
        raise Exception("kvm_intel kernel module not loaded, "
                        "nested VMs will fail to launch")

    try:
        nested_on = check_output("cat "
                                 "/sys/module/kvm_intel/parameters/nested"
                                 "".format(name),
                                 shell=True, stderr=STDOUT).decode('utf-8')
        if nested_on.strip() == 'Y':
            return
    except:
        log.exception("can't cat /sys/module/kvm_intel/parameters/nested")
        raise Exception("error inspecting kvm_intel module params, nested"
                        "VMs likely will not work")

    # need to unload and reload module
    try:
        check_call("modprobe -r kvm_intel", shell=True)
    except Exception as e:
        log.exception("couldn't unload kvm_intel: {}".format(e.output))
        raise Exception("Could not automatically unload kvm_intel module "
                        "to enable nested VMs. A manual reboot or reload"
                        "will be required.")

    if 0 != call("modprobe kvm_intel", shell=True):
        raise Exception("Could not automatically reload kvm_intel kernel"
                        "module to enable nested VMs. A manual reboot or "
                        "reload will be required.")
    return True


def copy_upstream_deb(src, dst):
    """ Copies local upstream debian package into container """
    shutil.copy(src, dst)


class Juju:
    """ various juju utilities """

    @classmethod
    def write_environments(cls, env_type, dst, owner, **kwargs):
        """ set juju environments for bootstrap
        """
        if 'password' not in kwargs:
            raise Exception("Required password not provided.")

        render_parts = {
            'openstack_password': kwargs['password'],
            'ubuntu_series': kwargs.get('series', 'trusty')
        }

        for opt in ['apt_proxy', 'apt_https_proxy', 'http_proxy',
                    'https_proxy']:
            val = kwargs.get(opt, None)
            if val:
                render_parts[opt] = val

        if env_type == "multi":
            maas_creds = kwargs.get('maascreds', None)
            if not maas_creds:
                raise Exception("Unable to read MAAS Credentials.")

            render_parts['maas_server'] = maas_creds['api_host']
            render_parts['maas_apikey'] = maas_creds['api_key']

        # configure juju environment for bootstrap
        env = load_template('juju-env/{}.yaml'.format(env_type))
        env_modified = env.render(render_parts)
        spew(dst, env_modified, owner=owner)


class CloudInit:
    """ Cloud-init specific utilities """

    @classmethod
    def set_userdata(cls, dst, **kwargs):
        """ set userdata file for container install

        :param str dst: Destination path to save userdata.yaml
        """
        render_parts = {'extra_sshkeys': [ssh_read_pubkey()]}

        upstream_ppa = kwargs.get('upstream_ppa', None)
        if upstream_ppa:
            render_parts['upstream_ppa'] = upstream_ppa

        render_parts['seed_command'] = CloudInit.proxy_pollinate()

        for opt in ['apt_proxy', 'apt_https_proxy', 'http_proxy'
                    'https_proxy', 'no_proxy', 'image_metadata_url',
                    'tools_metadata_url', 'apt_mirror']:
            val = kwargs.get(opt, None)
            if val:
                render_parts[opt] = val

        original_data = load_template('userdata.yaml')
        log.info("Prepared userdata: {}".format(render_parts))
        modified_data = original_data.render(render_parts)
        spew(dst, modified_data)

    @classmethod
    def proxy_pollinate(cls, **kwargs):
        """ Return pollinate cmd including http/s proxy if set """
        # pass proxy through to pollinate
        http_proxy = kwargs.get('http_proxy')
        https_proxy = kwargs.get('https_proxy')
        log.debug('Found proxy info: {}/{}'.format(http_proxy, https_proxy))
        pollinate = ['env']
        if http_proxy:
            pollinate.append('http_proxy={}'.format(http_proxy))
        if https_proxy:
            pollinate.append('https_proxy={}'.format(https_proxy))
        pollinate.extend(['pollinate', '-q'])
        return pollinate

    @classmethod
    def finished(cls, container, tries, maxlenient=20):
        """checks cloud-init result.json in container to find out status

        For the first `maxlenient` tries, it treats a container with
        no IP and SSH errors as non-fatal, assuming initialization is
        still ongoing. Afterwards, will raise exceptions for those
        errors, so as not to loop forever.

        returns True if cloud-init finished with no errors, False if
        it's not done yet, and raises an exception if it had errors.

        """
        cmd = 'sudo cat /run/cloud-init/result.json'
        try:
            result_json = container.run(cmd)

        except container.NoContainerIPException as e:
            log.debug("Container has no IPs according to lxc-info. "
                      "Will retry.")
            return False

        except container.ContainerRunException as e:
            _, returncode = e.args
            if returncode == 255:
                if tries < maxlenient:
                    log.debug("Ignoring initial SSH error.")
                    return False
                raise e
            if returncode == 1:
                # the 'cat' did not find the file.
                if tries < 1:
                    log.debug("Waiting for cloud-init status result")
                return False
            else:
                log.debug("Unexpected return code from reading "
                          "cloud-init status in container.")
                raise e

        if result_json == '':
            return False

        try:
            ret = json.loads(result_json)
        except Exception as e:
            if tries < maxlenient + 10:
                log.debug("exception trying to parse '{}'"
                          " - retrying".format(result_json))
                return False

            log.error(str(e))
            log.debug("exception trying to parse '{}'".format(result_json))
            raise e

        errors = ret['v1']['errors']
        if len(errors):
            log.error("Container cloud-init finished with "
                      "errors: {}".format(errors))
            raise Exception("Top-level container OS did not initialize "
                            "correctly.")
        return True
