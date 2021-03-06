"""
Simulacra cluster integration sub-package.


Copyright 2017 Josh Karpel

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import collections
import datetime
import hashlib
import itertools
import logging
import os
import pickle
import posixpath
import stat
import subprocess
import sys
import zlib
from copy import copy, deepcopy

import numpy as np  # needs to be here so that ask_for_eval works

import paramiko
from tqdm import tqdm

from . import core, vis, utils
from .units import *  # also for ask_for_eval


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

CmdOutput = collections.namedtuple('CmdOutput', ['stdin', 'stdout', 'stderr'])


class ClusterInterface:
    """
    A class for communicating with a user's home directory on a remote machine (as written, the UW CHTC HTCondor cluster). Should be used as a context manager.

    The remote home directory should be organized like:

    .. code::

       home/
       |-- backend/
       |-- jobs/
           |-- job1/
           |-- job2/

    """

    def __init__(self, remote_host, username, key_path,
                 local_mirror_root = 'cluster_mirror', remote_sep = '/'):
        self.remote_host = remote_host
        self.username = username
        self.key_path = key_path

        self.local_mirror_root = local_mirror_root
        self.remote_sep = remote_sep

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        self.ftp = None

    def __enter__(self):
        """Open the SSH and FTP connections."""
        self.ssh.connect(self.remote_host, username = self.username, key_filename = self.key_path)
        self.ftp = self.ssh.open_sftp()

        logger.info('Opened connection to {} as {}'.format(self.remote_host, self.username))

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close the SSH and FTP connections."""
        self.ftp.close()
        self.ssh.close()

        logger.info('Closed connection to {} as {}'.format(self.remote_host, self.username))

    def __str__(self):
        return 'Interface to {} as {}'.format(self.remote_host, self.username)

    def __repr__(self):
        return '{}(hostname = {}, username = {})'.format(self.__class__.__name__, self.remote_host, self.username)

    @property
    def local_home_dir(self):
        local_home = os.path.join(self.local_mirror_root, *self.remote_home_dir.split(self.remote_sep))

        return local_home

    def cmd(self, *cmds):
        """Run a list of commands sequentially on the remote host. Each command list begins in a totally fresh environment."""
        cmd_list = ['. ~/.profile', '. ~/.bash_profile'] + list(cmds)  # run the remote bash profile to pick up settings
        cmd = ';'.join(cmd_list)
        stdin, stdout, stderr = self.ssh.exec_command(cmd)

        return CmdOutput(stdin, stdout, stderr)

    @utils.cached_property
    def remote_home_dir(self):
        cmd_output = self.cmd('pwd')  # print name of home dir to stdout

        home_path = str(cmd_output.stdout.readline()).strip('\n')  # extract path of home dir from stdout

        logger.debug('Got home directory for {} on {}: {}'.format(self.username, self.remote_host, home_path))

        return home_path

    def get_job_status(self):
        """Get the status of jobs on the cluster."""
        cmd_output = self.cmd('condor_q -wide')

        status = cmd_output.stdout.readlines()

        status_str = 'Job Status:\n' + ''.join(status[1:])

        return status_str

    def remote_path_to_local_path(self, remote_path):
        """Convert a remote path to a local path."""
        return os.path.join(self.local_mirror_root, *remote_path.split(self.remote_sep))

    def get_file(self, remote_path, local_path, remote_stat = None, preserve_timestamps = True):
        """
        Download a file from the remote machine to the local machine.

        :param remote_path: the remote path to download
        :type remote_path: str
        :param local_path: the local path to place the downloaded file
        :type local_path: str
        :param remote_stat: the stat of the remote path (optimization, this method will fetch it if not passed in)
        :param preserve_timestamps: if True, copy the modification timestamps from the remote file to the local file
        :type preserve_timestamps: bool
        """
        utils.ensure_dir_exists(local_path)

        self.ftp.get(remote_path, local_path)

        if preserve_timestamps:
            if remote_stat is None:
                remote_stat = self.ftp.lstat(remote_path)
            os.utime(local_path, (remote_stat.st_atime, remote_stat.st_mtime))

        logger.debug('{}   <--   {}'.format(local_path, remote_path))

    def put_file(self, local_path, remote_path, preserve_timestamps = True):
        """
        Push a file from the local machine to the remote machine.

        :param local_path: the local path to push from
        :type local_path: str
        :param remote_path: the remove path to push to
        :type remote_path: str
        :param preserve_timestamps: if True, copy the modification timestamps from the remote file to the local file
        :type preserve_timestamps: bool
        """
        raise NotImplementedError

    def is_file_synced(self, remote_stat, local_path):
        """
        Determine whether a local file is the same as a remote file by checking the file size and modification times.

        Parameters
        ----------
        remote_stat
            The stat of the remote file.
        local_path
            The path to the local file.

        Returns
        -------
        :class:`bool`
            ``True`` if the file is synced, ``False`` otherwise.
        """
        if os.path.exists(local_path):
            local_stat = os.stat(local_path)
            if local_stat.st_size == remote_stat.st_size and local_stat.st_mtime == remote_stat.st_mtime:
                return True

        return False

    def mirror_file(self, remote_path, remote_stat, force_download = False, integrity_check = True):
        """
        Mirror a remote file, only downloading it if it does not match a local copy at a derived local path name.

        File integrity is checked by comparing the MD5 hash of the remote and local files.

        :param remote_path: the remote path to mirror
        :param remote_stat: the stat of the remote file
        :param force_download: if True, download the file even if it is synced
        :param integrity_check: if True, check that the MD5 hash of the remote and local files are the same, and redownload if they are not
        """
        local_path = self.remote_path_to_local_path(remote_path)
        if force_download or not self.is_file_synced(remote_stat, local_path):
            self.get_file(remote_path, local_path, remote_stat = remote_stat, preserve_timestamps = True)
            if integrity_check:
                output = self.cmd('openssl md5 {}'.format(remote_path))
                md5_remote = output.stdout.readline().split(' ')[1].strip()
                with open(local_path, mode = 'rb') as f:
                    md5_local = hashlib.md5()
                    md5_local.update(f.read())
                    md5_local = md5_local.hexdigest().strip()
                if md5_local != md5_remote:
                    logger.debug(f'MD5 hash on {self.remote_host} for file {remote_path} did not match local file at {local_path}, retrying')
                    self.mirror_file(remote_path, remote_stat, force_download = True)

    def walk_remote_path(self, remote_path, func_on_dirs = None, func_on_files = None, exclude_hidden = True, blacklist_dir_names = None, whitelist_file_ext = None):
        """
        Walk a remote directory starting at the given path.

        The functions func_on_dirs and func_on_files are passed the full path to the remote file and the ftp.stat of that file.

        :param remote_path: the remote path to start walking on
        :param func_on_dirs: the function to call on directories (takes the directory file path as an argument)
        :param func_on_files: the function to call on files (takes the file path as an argument)
        :param exclude_hidden: do not walk over hidden files or directories
        :param blacklist_dir_names: do not walk over directories with these names
        :param whitelist_file_ext: only walk over files with these extensions
        """
        if func_on_dirs is None:
            func_on_dirs = lambda *args: None
        if func_on_files is None:
            func_on_files = lambda *args: None

        # make sure each whitelisted file extension actually looks like a file extension
        cleaned = []
        for ext in whitelist_file_ext:
            if ext[0] != '.':
                clean = '.' + ext
            else:
                clean = ext
            cleaned.append(clean)
        whitelist_file_ext = tuple(cleaned)

        path_count = 0

        def walk(remote_path):
            for remote_stat in self.ftp.listdir_attr(remote_path):  # don't try to sort these, they're actually SFTPAttribute objects that don't have guaranteed attributes
                full_remote_path = posixpath.join(remote_path, remote_stat.filename)

                logger.debug('Checking remote path {}'.format(full_remote_path))

                # print a string that keeps track of the walked paths
                nonlocal path_count
                path_count += 1
                status_str = '\rPaths Found: {}'.format(path_count).ljust(25)
                status_str += '  |  '
                status_str += 'Current Path: {}'.format(full_remote_path).ljust(100)
                print(status_str, end = '')

                if not exclude_hidden or remote_stat.filename[0] != '.':
                    if stat.S_ISDIR(remote_stat.st_mode) and remote_stat.filename not in blacklist_dir_names:
                        func_on_dirs(full_remote_path, remote_stat)

                        logger.debug('Walking remote dir {}'.format(full_remote_path))
                        walk(full_remote_path)

                    elif stat.S_ISREG(remote_stat.st_mode) and full_remote_path.endswith(whitelist_file_ext):
                        func_on_files(full_remote_path, remote_stat)

        walk(remote_path)
        print()

    def mirror_remote_home_dir(self,
                               blacklist_dir_names = ('python', 'build_python'),
                               whitelist_file_ext = ('.txt', '.log', '.json', '.spec', '.sim', '.pkl')):
        """
        Mirror the entire remote home directory.

        Parameters
        ----------
        blacklist_dir_names
            Directories with these names will not be walked.
        whitelist_file_ext
            Only files with these file extensions will be transferred.
        """
        logger.info('Mirroring remote home directory')

        with utils.BlockTimer() as timer:
            self.walk_remote_path(self.remote_home_dir,
                                  func_on_files = self.mirror_file,
                                  func_on_dirs = lambda d, _: utils.ensure_dir_exists(d),
                                  blacklist_dir_names = blacklist_dir_names,
                                  whitelist_file_ext = whitelist_file_ext)

        logger.info(f'Mirroring complete. {timer}')


class SimulationResult:
    """A class that represents the results of Simulation run on a cluster."""

    def __init__(self, sim, job_processor):
        """
        Initialize a SimulationResult from a Simulation and JobProcessor, picking up information from both.

        Do not store direct references to the Simulation to ensure that the garbage collector can clean it up.

        :param sim: a Simulation
        :param job_processor: a JobProcessor
        """
        self.name = copy(sim.name)
        self.file_name = copy(int(sim.file_name))
        self.plots_dir = job_processor.plots_dir

        self.init_time = copy(sim.init_time)
        self.start_time = copy(sim.start_time)
        self.end_time = copy(sim.end_time)
        self.elapsed_time = copy(sim.elapsed_time.total_seconds())
        self.running_time = copy(sim.running_time.total_seconds())


class JobProcessor(core.Beet):
    """
    A class that processes a collection of pickled Simulations. Should be subclassed for specialization.

    Attributes
    ----------
    running_time
        The total running time of all the simulations in the job.
    elapsed_time
        The elapsed time of the job (first simulation started to last simulation ended).
    """

    simulation_result_type = SimulationResult

    def __init__(self, job_name, job_dir_path, simulation_type):
        """
        Parameters
        ----------
        job_name : :class:`str`
            The name of the job.
        job_dir_path : :class:`str`
            The path to the job directory.
        simulation_type
            The type of the Simulation used in the job.
        """
        super().__init__(job_name)

        self.job_dir_path = job_dir_path

        for directory in (self.inputs_dir, self.outputs_dir, self.plots_dir, self.movies_dir, self.summaries_dir):
            utils.ensure_dir_exists(directory)

        self.sim_names = self.get_sim_names_from_specs()
        self.sim_count = len(self.sim_names)
        self.unprocessed_sim_names = set(self.sim_names)

        self.data = collections.OrderedDict((sim_name, None) for sim_name in self.sim_names)

        self.simulation_type = simulation_type

    def __str__(self):
        return '{} for job {}, processed {}/{} Simulations'.format(self.__class__.__name__, self.name, self.sim_count - len(self.unprocessed_sim_names), self.sim_count)

    @property
    def inputs_dir(self):
        return os.path.join(self.job_dir_path, 'inputs')

    @property
    def outputs_dir(self):
        return os.path.join(self.job_dir_path, 'outputs')

    @property
    def plots_dir(self):
        return os.path.join(self.job_dir_path, 'plots')

    @property
    def movies_dir(self):
        return os.path.join(self.job_dir_path, 'movies')

    @property
    def summaries_dir(self):
        return os.path.join(self.job_dir_path, 'summaries')

    @property
    def running_time(self):
        return datetime.timedelta(seconds = sum(r.running_time for r in self.data.values() if r is not None))

    @property
    def elapsed_time(self):
        earliest = min(r.init_time for r in self.data.values() if r is not None)
        latest = max(r.end_time for r in self.data.values() if r is not None)

        return latest - earliest

    def get_sim_names_from_specs(self):
        """Get a list of Simulation file names based on their Specifications."""
        return sorted([f.strip('.spec') for f in os.listdir(self.inputs_dir)], key = int)

    def get_sim_names_from_sims(self):
        """Get a list of Simulation file names actually found in the output directory."""
        return sorted([f.strip('.sim') for f in os.listdir(self.outputs_dir)], key = int)

    def save(self, target_dir = None, file_extension = '.job', **kwargs):
        """

        Parameters
        ----------
        target_dir
        file_extension
        kwargs

        Returns
        -------
        """
        return super(JobProcessor, self).save(target_dir = target_dir, file_extension = file_extension, **kwargs)

    def _load_sim(self, sim_file_name, **load_kwargs):
        """
        Load a :class:`Simulation` by its ``file_name``.

        Parameters
        ----------
        sim_file_name : :class:`str`
            The ``file_name`` of the :class:`Simulation` to load.
        load_kwargs
            Keyword arguments are passed to the ``load`` method of the :class:`Simulation``.

        Returns
        -------
        :class:`Simulation`
            The loaded :class:`Simulation`.
        """
        sim = None
        sim_path = os.path.join(self.outputs_dir, '{}.sim'.format(sim_file_name))

        try:
            sim = self.simulation_type.load(os.path.join(sim_path), **load_kwargs)

            if sim.status != 'finished':
                raise FileNotFoundError

            logger.debug('Loaded {}.sim from job {}'.format(sim_file_name, self.name))
        except (FileNotFoundError, EOFError) as e:
            logger.debug('Failed to find completed {}.sim from job {} due to {}'.format(sim_file_name, self.name, e))
        except zlib.error as e:
            logger.warning('Encountered zlib error while trying to read {}.sim from job {}: {}'.format(sim_file_name, self.name, e))
            os.remove(sim_path)
        except Exception as e:
            logger.exception('Exception encountered while trying to find completed {}.sim from job {} due to {}'.format(sim_file_name, self.name, e))
            raise e

        return sim

    def load_sims(self, force_reprocess = False):
        """
        Process the job by loading newly-downloaded Simulations and generating SimulationResults from them.

        :param force_reprocess: if True, process all Simulations in the output directory regardless of prior processing status
        """
        with utils.BlockTimer() as t:
            logger.info('Loading simulations from job {}'.format(self.name))

            if force_reprocess:
                sim_names = tqdm(copy(self.sim_names))
            else:
                new_sims = self.unprocessed_sim_names.intersection(self.get_sim_names_from_sims())  # only process newly-downloaded Simulations
                sim_names = tqdm(new_sims)

            for sim_name in sim_names:
                sim = self._load_sim(sim_name)

                if sim is not None and sim.status == core.STATUS_FIN:
                    try:
                        self.data[sim_name] = self.simulation_result_type(sim, job_processor = self)
                        self.unprocessed_sim_names.discard(sim_name)
                    except AttributeError:
                        logger.exception('Exception encountered while processing simulation {}'.format(sim_name))

                self.save(target_dir = self.job_dir_path)

        logger.info('Finished loading simulations from job {}. Failed to find {} / {} simulations. Elapsed time: {}'.format(self.name, len(self.unprocessed_sim_names), self.sim_count, t.wall_time_elapsed))

    def summarize(self):
        with utils.BlockTimer() as t:
            if len(self.unprocessed_sim_names) < self.sim_count:
                self.make_time_diagnostics_plot()
                self.write_time_diagnostics_to_file()

            # self.write_to_txt()
            # self.write_to_csv()

            self.make_summary_plots()

        logger.info('Finished summaries for job {}. Elapsed time: {}'.format(self.name, t.wall_time_elapsed))

    def write_to_csv(self):
        raise NotImplementedError

    def write_to_txt(self):
        raise NotImplementedError

    def select_by_kwargs(self, **kwargs):
        """
        Return all of the :class:`SimulationResult` that match the key-value pairs passed as keyword arguments.

        Parameters
        ----------
        kwargs
            Key-value pairs to match against.

        Returns
        -------

        """
        out = []

        for sim_result in (r for r in self.data.values() if r is not None):
            if all(getattr(sim_result, key) == val for key, val in kwargs.items()):
                out.append(sim_result)

        return out

    def select_by_lambda(self, test_function):
        """
        Return all of the :class:`SimulationResult` for which ``test_function(sim_result)`` is True.

        Parameters
        ----------
        test_function : callable
            A test function that will be called on simulation results to determine whether they should be in the result set.

        Returns
        -------

        """
        return list([sim_result for sim_result in self.data.values() if test_function(sim_result) and sim_result is not None])

    @utils.memoize
    def parameter_set(self, parameter):
        """Get the set of values of a parameter from the collected data."""
        return set(getattr(result, parameter) for result in self.data.values())

    def make_summary_plots(self):
        """Hook method for making automatic summary plots from collected data."""
        pass

    def write_time_diagnostics_to_file(self):
        """Write time diagnostic information for the job to a text file in the job directory."""
        path = os.path.join(self.job_dir_path, f'{self.name}_diagnostics.txt')
        with open(path, mode = 'w') as f:
            f.write('\n'.join((
                f'Diagnostic Data for {self.name}:',
                '',
                f'{self.sim_count - len(self.unprocessed_sim_names)} {self.simulation_type.__name__}s',
                f'Simulation Result Type: {self.simulation_result_type.__name__}',
                '',
                f'Elapsed Time: {self.elapsed_time}',
                f'Combined Runtime: {self.running_time}',
                f'Speedup Factor: {uround(self.running_time / self.elapsed_time)}',
                '',
                f'Earliest Sim Init: {min(r.init_time for r in self.data.values() if r is not None)}',
                f'Latest Sim Init: {max(r.init_time for r in self.data.values() if r is not None)}',
                f'Earliest Sim Start: {min(r.start_time for r in self.data.values() if r is not None)}',
                f'Latest Sim Start: {max(r.start_time for r in self.data.values() if r is not None)}',
                f'Earliest Sim Finish: {min(r.end_time for r in self.data.values() if r is not None)}',
                f'Latest Sim Finish: {max(r.end_time for r in self.data.values() if r is not None)}',
            )))

        logger.debug(f'Wrote diagnostic information for job {self.name} to {path}')

    def make_time_diagnostics_plot(self):
        """Save a diagnostics plot to the job directory.."""

        sim_numbers = [result.file_name for result in self.data.values() if result is not None]
        running_time = [result.running_time for result in self.data.values() if result is not None]

        vis.xy_plot(f'{self.name}__diagnostics',
                    sim_numbers,
                    running_time,
                    line_kwargs = [dict(linestyle = '', marker = '.')],
                    y_unit = 'hours',
                    x_label = 'Simulation Number', y_label = 'Time',
                    title = f'{self.name} Diagnostics',
                    target_dir = self.summaries_dir)

        logger.debug(f'Generated diagnostics plot for job {self.name}')


def combine_job_processors(*job_processors, job_dir_path = None):
    """

    JobProcessor and Simulation types are inherited from the first JobProcessor in the arguments

    :param job_processors:
    :return:
    """
    sim_type = job_processors[0].simulation_type
    jp_type = job_processors[0].__class__
    combined_jp = jp_type(name = '-'.join(jp.name for jp in job_processors),
                          job_dir_path = job_dir_path,
                          simulation_type = sim_type)

    combined_jp.data = collections.OrderedDict((ii, copy(sim_result)) for ii, (sim_name, sim_result) in enumerate(itertools.chain(jp.data for jp in job_processors)))

    return combined_jp


class Parameter:
    """A class that represents a parameter of a :class:`Specification`."""

    name = utils.Typed('name', legal_type = str)
    expandable = utils.Typed('expandable', legal_type = bool)

    def __init__(self, name, value = None, expandable = False):
        """
        Parameters
        ----------
        name : :class:`str`
            The name of the Parameter, which should match a keyword argument of the target :class:`Specification`.
        value : :class:`str`
            The value of the Parameter, or an iterable of values.
        expandable : :class:`Bool`
            If True, :func:`expand_parameters_to_dicts` will expand along an iterable `value`.
        """
        self.name = name
        self.value = value
        self.expandable = expandable

    def __str__(self):
        return '{} {} = {}'.format(self.__class__.__name__, self.name, self.value)

    def __repr__(self):
        return '{}(name = {}, value = {})'.format(self.__class__.__name__, self.name, self.value)


def expand_parameters_to_dicts(parameters):
    """
    Expand an iterable of :class:`Parameter` to a list of dictionaries containing all of the combinations of parameter values.
    Each of these dictionaries can then be unpacked into a :class:`Specification`.

    If a :class:`Parameter` has ``expandable = True``, it will be expanded across the values in the outermost iterable in that :class:`Parameter`'s ``value``.

    Parameters
    ----------
    parameters : iterable of :class:`Parameter`
        The parameters to expand over.

    Returns
    -------
    iterable of :class:`dict`
        An iterable of dictionaries containing all of the combinations of parameters.
    """
    dicts = [collections.OrderedDict()]

    for par in parameters:
        if par.expandable and hasattr(par.value, '__iter__') and not isinstance(par.value, str) and hasattr(par.value, '__len__'):  # make sure the value is an iterable that isn't a string and has a length
            dicts = [deepcopy(d) for d in dicts for _ in range(len(par.value))]
            for d, v in zip(dicts, itertools.cycle(par.value)):
                d[par.name] = v
        else:
            for d in dicts:
                d[par.name] = par.value

    return dicts


def ask_for_input(question, default = None, cast_to = str):
    """
    Ask for input from the user, with a default value, which will be cast to a specified type.

    Parameters
    ----------
    question : :class:`str`
        A string to display on the command prompt for the user.
    default
        The default answer to the question.
    cast_to
        A type to cast the user's input to.

    Returns
    -------

    """
    try:
        input_str = input(question + ' [Default: {}] > '.format(default))

        trimmed = input_str.replace(' ', '')
        if trimmed == '':
            out = cast_to(default)
        else:
            out = cast_to(trimmed)

        logger.debug('Got input from stdin for question "{}": {}'.format(question, out))

        return out
    except Exception as e:
        print(e)
        ask_for_input(question, default = default, cast_to = cast_to)


def ask_for_bool(question, default = False):
    """
    Ask for input from the user, with a default value, which will be interpreted as a boolean.

    Synonyms for True: 'true', 't', 'yes', 'y', '1', 'on'
    Synonyms for False: 'false', 'f', 'no', 'n', '0', 'off'

    Parameters
    ----------
    question : :class:`str`
        A string to display on the command prompt for the user.
    default : :class:`str`
        The default answer to the question.

    Returns
    -------
    :class:`Bool`
        The input, interpreted as a boolean.
    """
    try:
        input_str = input(question + ' [Default: {}] > '.format(default))

        trimmed = input_str.replace(' ', '')
        if trimmed == '':
            input_str = str(default)

        logger.debug('Got input from stdin for question "{}": {}'.format(question, input_str))

        input_str_lower = input_str.lower()
        if input_str_lower in ('true', 't', 'yes', 'y', '1', 'on'):
            return True
        elif input_str_lower in ('false', 'f', 'no', 'n', '0', 'off'):
            return False
        else:
            raise ValueError('Invalid answer to question "{}"'.format(question))
    except Exception as e:
        print(e)
        ask_for_bool(question, default = default)


def ask_for_eval(question, default = 'None'):
    """
    Ask for input from the user, with a default value, which will be evaluated as a Python command.

    Numpy's top-level interface (imported as np) and Simulacra's unit module (* imported) are both available. For example, ``'np.linspace(0, twopi, 100)'`` will produce the expected result.

    NB: this function is not safe! The user can execute arbitrary Python code.

    Parameters
    ----------
    question : :class:`str`
        A string to display on the command prompt for the user.
    default : :class:`str`
        The default answer to the question.

    Returns
    -------

    """
    input_str = input(question + ' [Default: {}] (eval) > '.format(default))

    trimmed = input_str.replace(' ', '')
    if trimmed == '':
        input_str = str(default)

    logger.debug('Got input from stdin for question "{}": {}'.format(question, input_str))

    # print(input_str)

    try:
        return eval(input_str)
    except NameError as e:
        # print(e)
        did_you_mean = ask_for_bool("Did you mean '{}'?".format(input_str), default = 'yes')
        if did_you_mean:
            return eval("'{}'".format(input_str))
        else:
            raise e
    except Exception as e:
        print(e)
        ask_for_eval(question, default = default)


def abort_job_creation():
    """Abort job creation by exiting the script."""
    print('Aborting job creation...')
    logger.critical('Aborted job creation')
    sys.exit(0)


def create_job_subdirs(job_dir):
    """Create directories for the inputs, outputs, logs, and movies."""
    print('Creating job directory and subdirectories...')

    utils.ensure_dir_exists(job_dir)
    utils.ensure_dir_exists(os.path.join(job_dir, 'inputs'))
    utils.ensure_dir_exists(os.path.join(job_dir, 'outputs'))
    utils.ensure_dir_exists(os.path.join(job_dir, 'logs'))
    utils.ensure_dir_exists(os.path.join(job_dir, 'movies'))


def save_specifications(specifications, job_dir):
    """Save a list of Specifications."""
    print('Saving Specifications...')

    for spec in specifications:
        spec.save(target_dir = os.path.join(job_dir, 'inputs/'))

    logger.debug('Saved Specifications')


def write_specifications_info_to_file(specifications, job_dir):
    """Write information from the list of the Specifications to a file."""
    print('Writing Specification info to file...')

    with open(os.path.join(job_dir, 'specifications.txt'), 'w') as file:
        for spec in specifications:
            file.write(str(spec.info()) + '\n')

    logger.debug('Saved Specification information')


def write_parameters_info_to_file(parameters, job_dir):
    """Write information from the list of Parameters to a file."""
    print('Writing parameters to file...')

    with open(os.path.join(job_dir, 'parameters.txt'), 'w') as file:
        for param in parameters:
            file.write(repr(param) + '\n')

    logger.debug('Saved parameter information')


CHTC_SUBMIT_STRING = """
universe = vanilla
log = logs/cluster_$(Cluster).log
error = logs/$(Process).err
#
executable = /home/karpel/backend/run_sim.sh
arguments = $(Process)
#
should_transfer_files = YES
when_to_transfer_output = ON_EXIT_OR_EVICT
transfer_input_files = http://proxy.chtc.wisc.edu/SQUID/karpel/python.tar.gz, /home/karpel/backend/simulacra.tar.gz, /home/karpel/backend/ionization.tar.gz, /home/karpel/backend/run_sim.py, inputs/$(Process).spec
transfer_output_remaps = "$(Process).sim = outputs/$(Process).sim ; $(Process).log = logs/$(Process).log ; $(Process).mp4 = outputs/$(Process).mp4"
#
+JobBatchName = "{batch_name}"
#
+is_resumable = {checkpoints}
+WantGlideIn = {flockglide}
+WantFlocking = {flockglide}
#
skip_filechecks = true
max_materialize = {max_materialize}
#
on_exit_remove = (ExitBySignal == False) && (ExitCode == 0)
#
request_cpus = 1
request_memory = {memory}GB
request_disk = {disk}GB
#
requirements = (OpSysMajorVer == 6) || (OpSysMajorVer == 7)
#
queue {num_jobs}
"""


def format_chtc_submit_string(job_name, specification_count, checkpoints = True):
    """
    Return a formatted submit string for an HTCondor job.

    :param job_name: the name of the job
    :param specification_count: the number of Specifications in the job
    :param checkpoints: if the Simulations are going to use checkpoints, this should be True
    :return: an HTCondor submit string
    """
    fmt = dict(
            batch_name = ask_for_input('Job batch name?', default = job_name, cast_to = str),
            checkpoints = str(checkpoints).lower(),
            flockglide = str(ask_for_bool('Flock and Glide?', default = 'y')).lower(),
            memory = ask_for_input('Memory (in GB)?', default = 4, cast_to = float),
            disk = ask_for_input('Disk (in GB)?', default = 10, cast_to = float),
            num_jobs = specification_count,
            max_materialize = ask_for_input('Max Materialize?', default = 1000, cast_to = int),
    )

    return CHTC_SUBMIT_STRING.format(**fmt).strip()


def specification_check(specifications, check = 3):
    """Ask the user whether some number of specifications look correct."""
    print('Generated {} Specifications'.format(len(specifications)))

    for s in specifications[0:check]:
        print('-' * 20)
        print(s)
        print(s.info())
        print('-' * 20)

    check = ask_for_bool('Do the first {} Specifications look correct?'.format(check), default = 'No')
    if not check:
        abort_job_creation()


def submit_check(submit_string):
    """Ask the user whether the submit string looks correct."""
    print('-' * 20)
    print(submit_string)
    print('-' * 20)

    check = ask_for_bool('Does the submit file look correct?', default = 'No')
    if not check:
        abort_job_creation()


def write_submit_file(submit_string, job_dir):
    """Write the submit string to a file."""
    print('Saving submit file...')

    with open(os.path.join(job_dir, 'submit_job.sub'), mode = 'w') as file:
        file.write(submit_string)

    logger.debug('Saved submit file')


def write_job_info_to_file(job_info, job_dir):
    """Write job information to a file."""
    with open(os.path.join(job_dir, 'info.pkl'), mode = 'wb') as f:
        pickle.dump(job_info, f, protocol = -1)


def load_job_info_from_file(job_dir):
    """Load job information from a file."""
    with open(os.path.join(job_dir, 'info.pkl'), mode = 'rb') as f:
        return pickle.load(f)


def submit_job(job_dir):
    """Submit a job using a pre-existing submit file."""
    print('Submitting job...')

    os.chdir(job_dir)

    subprocess.run(['condor_submit', 'submit_job.sub'])
    # subprocess.run(['condor_submit', 'submit_job.sub', '-factory'])
