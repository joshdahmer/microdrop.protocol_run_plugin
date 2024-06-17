import logging

import gtk
from microdrop.plugin_helpers import AppDataController, StepOptionsController
from microdrop.plugin_manager import (IPlugin, Plugin, implements, emit_signal,
                                      get_service_instance_by_name,
                                      PluginGlobals, ScheduleRequest)
from flatland import Integer, Float, Form, Enum, Boolean
from microdrop.app_context import get_app
import path_helpers as ph
import trollius as asyncio
import win32pipe
import win32file
from pygtkhelpers.gthreads import gtk_threadsafe
import threading

from ._version import get_versions

__version__ = get_versions()['version']
del get_versions

logger = logging.getLogger(__name__)

# Add plugin to `"microdrop.managed"` plugin namespace.
PluginGlobals.push_env('microdrop.managed')


class ProtocolRunPlugin(AppDataController, StepOptionsController, Plugin):
    '''
    This class is automatically registered with the PluginManager.
    '''
    implements(IPlugin)

    plugin_name = str(ph.path(__file__).realpath().parent.name)
    try:
        version = __version__
    except NameError:
        version = 'v0.0.0+unknown'

    AppFields = None

    StepFields =  Form.of(Boolean.named('Wait')
                         .using(default=False, optional=True))

    def __init__(self):
        super(ProtocolRunPlugin, self).__init__()
        # The `name` attribute is required in addition to the `plugin_name`
        # attribute because MicroDrop uses it for plugin labels in, for
        # example, the plugin manager dialog.
        self.name = self.plugin_name
        self.pipe_name = "\\\\.\\pipe\\csharp_pipe"
        self.pipe_handle = win32pipe.CreateNamedPipe(self.pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE|win32pipe.PIPE_READMODE_MESSAGE|win32pipe.PIPE_WAIT,
            1,65536,65536,300,None)

    def get_schedule_requests(self, function_name):
        """
        .. versionchanged:: 2.5
            Enable _after_ command plugin and zmq hub to ensure command can be
            registered.

        .. versionchanged:: 2.5.3
            Remove scheduling requests for deprecated `on_step_run()` method.
        """
        if function_name == 'on_plugin_enable':
            return [ScheduleRequest('dmf_device_ui_plugin', self.name),
                    ScheduleRequest('dropbot_plugin', self.name),
                    ScheduleRequest('mr_box_plugin', self.name)]
        elif function_name == 'on_protocol_finished':
            return [ScheduleRequest('dropbot_plugin', self.name)]
        return []


    def on_plugin_enable(self):
        '''
        Handler called when plugin is enabled.

        For example, when the MicroDrop application is **launched**, or when
        the plugin is **enabled** from the plugin manager dialog.
        '''
        app = get_app()
        logger.info("waiting for connection")
        thread = threading.Thread(target=self.initiate_pipe)
        thread.daemon= True
        thread.start()
        #connection_message = self.initiate_pipe()
        #logger.info(connection_message)

        try:
            super(ProtocolRunPlugin, self).on_plugin_enable()
        except AttributeError:
            pass

    def on_plugin_disable(self):
        '''
        Handler called when plugin is disabled.

        For example, when the MicroDrop application is **closed**, or when the
        plugin is **disabled** from the plugin manager dialog.
        '''
        try:
            super(ProtocolRunPlugin, self).on_plugin_disable()
        except AttributeError:
            pass

    @asyncio.coroutine
    def on_step_run(self, plugin_kwargs, signals):
        '''
        Handler called whenever a step is executed.

        Plugins that handle this signal **MUST** emit the ``on_step_complete``
        signal once they have completed the step.  The protocol controller will
        wait until all plugins have completed the current step before
        proceeding.
        '''
        # Get latest step field values for this plugin.
        options = plugin_kwargs[self.name]
        # Apply step options
        self.apply_step_options(options)

    def apply_step_options(self, step_options):
        '''
        Apply the specified step options.


        Parameters
        ----------
        step_options : dict
            Dictionary containing the MR-Box peripheral board plugin options
            for a protocol step.
        '''
        # app = get_app()
        # app_values = self.get_app_values()

        if step_options.get("Wait"):
            logger.info("Waiting for response from pipe to continue Protocol")
            message = 'Protocol Paused\n'
            self.send_pipe_message(self.pipe_handle, message)
            logger.info("Waiting for response")
            pipe_message =self.read_pipe_message(self.pipe_handle)
            logger.info(pipe_message)
            message = 'Protocol Continuing\n'
            self.send_pipe_message(self.pipe_handle, message)
            logger.info(message)


    def run_protocol_from_plugin(self, protocol_location, widget = None):
        app = get_app()
        # protocol_path = ph.path(protocol_location)
        app.protocol_controller.load_protocol(protocol_location)
        app.protocol_controller.run_protocol()

    def initiate_pipe(self):
        logger.info("in thread")
        win32pipe.ConnectNamedPipe(self.pipe_handle, None)
        logger.info("Client connected")
        pipe_message = self.read_pipe_message(self.pipe_handle)
        logger.info(pipe_message)
        app = get_app()
        app.protocol_controller.load_protocol(pipe_message)
        app.protocol_controller.run_protocol()
        message = 'Protocol running\n'
        self.send_pipe_message(self.pipe_handle, message)
        logger.info(message)

    def read_pipe_message(self, pipe_handle):
        (_, read_message) = win32file.ReadFile(pipe_handle, 1000)
        read_message = read_message.decode("utf-8")
        read_message = read_message[:-2]
        return read_message

    def send_pipe_message(self, pipe_handle, message):
        ret, length = win32file.WriteFile(pipe_handle, message.encode())
        logger.info('{0}, {1} from writefile'.format(ret, length))
        win32file.FlushFileBuffers(pipe_handle)


    def on_protocol_finished(self):
        """
        Reconnect to the pipe after completing a protocol to tell the C# program that the protocol is done
        and check if a new protocol needs to be run
        """
        app = get_app()
        logger.info("waiting for connection")
        thread = threading.Thread(target=self.reenter_pipe_thread)
        thread.daemon= True
        thread.start()

    def reenter_pipe_thread(self):
        logger.info("re-entering pipe thread")
        message = 'Protocol Complete. Start another?\n'
        self.send_pipe_message(self.pipe_handle, message)
        logger.info("Waiting for response")
        pipe_message = self.read_pipe_message(self.pipe_handle)
        logger.info(pipe_message)

        if pipe_message is not "No":
            app = get_app()
            app.protocol_controller.load_protocol(pipe_message)
            app.protocol_controller.run_protocol()
            message = 'Protocol Running\n'
            self.send_pipe_message(self.pipe_handle, message)
            logger.info(message)
        else:
            message = 'Closing pipe\n'
            self.send_pipe_message(self.pipe_handle, message)
            logger.info(message)
            win32pipe.DisconnectNamedPipe(self.pipe_handle)
            self.pipe_handle.close()
            logger.info("Pipe closed")

PluginGlobals.pop_env()
