import datetime as dt
import logging
import os

from compy import utils

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class Specification(utils.Beet):
    """
    A class that contains the information necessary to run a simulation.

    It should be subclassed for each type of simulation and all additional information necessary to run that kind of simulation should be added via keyword arguments.
    """

    def __init__(self, name, file_name = None, simulation_type = None, **kwargs):
        """
        Construct a Specification.

        Any number of additional keyword arguments can be passed. They will be stored in an attribute called extra_args.

        :param name: the internal name of the Specification
        :param file_name: the desired external name, used for pickling. Illegal characters are stripped before use.
        :param kwargs: extra arguments, stored as attributes
        """
        super(Specification, self).__init__(name, file_name = file_name)

        self.simulation_type = simulation_type

        for k, v in kwargs.items():
            setattr(self, k, v)

    def save(self, target_dir = None, file_extension = '.spec'):
        """
        Atomically pickle the Specification to {target_dir}/{self.file_name}.{file_extension}, and gzip it for reduced disk usage.

        :param target_dir: directory to save the Specification to
        :param file_extension: file extension to name the Specification with
        :return: None
        """
        super(Specification, self).save(target_dir, file_extension)

    def to_simulation(self):
        return self.simulation_type(self)


class Simulation(utils.Beet):
    """
    A class that represents a simulation.

    It should be subclassed and customized for each variety of simulation.
    Ideally, actual computation should be handed off to another object, while the Simulation itself stores the data produced by that object.
    """

    status = utils.RestrictedValues('status', {'initialized', 'running', 'finished', 'paused'})

    def __init__(self, spec, initial_status = 'initialized'):
        """
        Construct a Simulation from a Specification.

        :param spec: the Specification for the Simulation
        :param initial_status: an initial status for the simulation, defaults to 'initialized'
        """
        self.spec = spec
        self.status = initial_status

        super(Simulation, self).__init__(spec.name, file_name = spec.file_name)  # inherit name and file_name from spec
        self.spec.simulation_type = self.__class__

        # diagnostic data
        self.restarts = 0
        self.start_time = dt.datetime.now()
        self.end_time = None
        self.elapsed_time = None
        self.latest_load_time = dt.datetime.now()
        self.run_time = dt.timedelta()

    def save(self, target_dir = None, file_extension = '.sim'):
        """
        Atomically pickle the Simulation to {target_dir}/{self.file_name}.{file_extension}, and gzip it.

        :param target_dir: directory to save the Simulation to
        :param file_extension: file extension to name the Simulation with
        :return: None
        """
        if self.status != 'finished':
            self.run_time += dt.datetime.now() - self.latest_load_time
            self.latest_load_time = dt.datetime.now()

        return super(Simulation, self).save(target_dir, file_extension)

    @classmethod
    def load(cls, file_path):
        """
        Load a Simulation from file_path.

        :param file_path: the path to try to load a Simulation from
        :return: the loaded Simulation
        """
        sim = super(Simulation, cls).load(file_path)

        sim.latest_load_time = dt.datetime.now()
        if sim.status != 'finished':
            sim.restarts += 1

        return sim

    def __str__(self):
        return '{}: {} ({}) [{}]  |  {}'.format(self.__class__.__name__, self.name, self.file_name, self.uid, self.spec)

    def __repr__(self):
        return '{}(spec = {}, uid = {})'.format(self.__class__.__name__, repr(self.spec), self.uid)

    def run_simulation(self):
        raise NotImplementedError

    def info(self):
        diag = ['Status: {}'.format(self.status),
                '   Start Time: {}'.format(self.start_time),
                '   Latest Load Time: {}'.format(self.latest_load_time),
                '   End Time: {}'.format(self.end_time),
                '   Elapsed Time: {}'.format(self.elapsed_time),
                '   Run Time: {}'.format(self.run_time)]

        return '\n'.join((str(self), *diag, self.spec.info()))


class Animator:
    """
    A class that handles sending frames to ffmpeg to create animations.

    ffmpeg must be visible on the system path.
    """

    def __init__(self, postfix = '', target_dir = None,
                 length = 30, fps = 30,
                 colormap = plt.cm.inferno):
        if target_dir is None:
            target_dir = os.getcwd()
        self.target_dir = target_dir

        postfix = cp.utils.strip_illegal_characters(postfix)
        if postfix != '' and not postfix.startswith('_'):
            postfix = '_' + postfix
        self.postfix = postfix

        self.length = length
        self.fps = fps
        self.colormap = colormap

        self.redraw = []

        self.sim = None
        self.spec = None
        self.fig = None

    def __str__(self):
        return '{}(postfix = "{}")'.format(self.__class__.__name__, self.postfix)

    def __repr__(self):
        return '{}(postfix = {})'.format(self.__class__.__name__, self.postfix)

    def initialize(self, simulation):
        """Hook for second part of initialization, once the Simulation is known."""
        self.sim = simulation
        self.spec = simulation.spec

        self.file_name = '{}{}.mp4'.format(self.sim.file_name, self.postfix)
        self.file_path = os.path.join(self.target_dir, self.file_name)
        cp.utils.ensure_dir_exists(self.file_path)
        try:
            os.remove(self.file_path)
        except FileNotFoundError:
            pass

        ideal_frame_count = self.length * self.fps
        self.decimation = int(self.sim.time_steps / ideal_frame_count)
        if self.decimation < 1:
            self.decimation = 1
        self.fps = (self.sim.time_steps / self.decimation) / self.length

        self._initialize_figure()

        self.fig.canvas.draw()
        self.background = self.fig.canvas.copy_from_bbox(self.fig.bbox)
        canvas_width, canvas_height = self.fig.canvas.get_width_height()
        self.cmdstring = ("ffmpeg",
                          '-y',
                          '-r', '{}'.format(self.fps),  # choose fps
                          '-s', '%dx%d' % (canvas_width, canvas_height),  # size of image string
                          '-pix_fmt', 'argb',  # format
                          '-f', 'rawvideo', '-i', '-',  # tell ffmpeg to expect raw video from the pipe
                          '-vcodec', 'mpeg4',  # output encoding
                          '-q:v', '1',
                          self.file_path)

        self.ffmpeg = subprocess.Popen(self.cmdstring, stdin = subprocess.PIPE, bufsize = -1)

        logger.info('Initialized {}'.format(self))

    def cleanup(self):
        self.ffmpeg.communicate()
        logger.info('Cleaned up {}'.format(self))

    def _initialize_figure(self):
        logger.debug('Initialized figure for {}'.format(self))

    def _update_data(self):
        logger.debug('{} updated data from {} {}'.format(self, self.sim.__class__.__name__, self.sim.name))

    def _redraw_frame(self):
        plt.set_cmap(self.colormap)

        self.fig.canvas.restore_region(self.background)

        self._update_data()
        for rd in self.redraw:
            self.fig.draw_artist(rd)

        self.fig.canvas.blit(self.fig.bbox)

        logger.debug('Redrew frame for {}'.format(self))

    def send_frame_to_ffmpeg(self):
        self._redraw_frame()
        self.ffmpeg.stdin.write(self.fig.canvas.tostring_argb())

        logger.debug('{} sent frame to ffpmeg from {} {}'.format(self, self.sim.__class__.__name__, self.sim.name))
