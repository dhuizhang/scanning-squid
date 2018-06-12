import os
import sys

import matplotlib.pyplot as plt
import numpy as np
import json
from typing import Dict, List, Sequence, Any, Union

import qcodes as qc
from qcodes.station import Station
from qcodes.instrument_drivers.stanford_research.SR830 import SR830

import nidaqmx
from nidaqmx.constants import AcquisitionType

import squids
import atto
import utils
from scanner import Scanner
from daq import DAQAnalogInputs
from plots import ScanPlot

from pint import UnitRegistry
ureg = UnitRegistry()
#: Tell UnitRegistry instance what a Phi0 is, and that Ohm = ohm
with open('squid_units.txt', 'w') as f:
    f.write('Phi0 = 2.067833831e-15 * Wb\n')
    f.write('Ohm = ohm\n')
ureg.load_definitions('./squid_units.txt')

import logging
log = logging.getLogger(__name__)


class Microscope(Station):
    """Base class for scanning SQUID microscope.
    """
    def __init__(self, config_file: str, temp: str, ureg: Any=ureg, log_level: Any=logging.INFO,
                 log_name: str=None, **kwargs) -> None:
        """
        Args:
            config_file: Path to microscope configuration JSON file.
            temp: 'LT' or 'RT', depending on whether the microscope is cold or not.
                Sets the voltage limits for the scanner and Attocubes.
            ureg: pint UnitRegistry for managing physical units.
            log_level: e.g. logging.DEBUG or logging.INFO
            log_name: Log file will be saved as logs/{log_name}.log.
                Default is the name of the microscope configuration file.
            **kwargs: Keyword arguments to be passed to Station constructor.
        """
        super().__init__(**kwargs)
        with open(config_file) as f:
            self.config = json.load(f)
        if not os.path.exists('logs'):
            os.mkdir('logs')
        if log_name is None:
            log_file = utils.next_file_name('./logs/' + config_file.split('.')[0], 'log')
        else:
            log_file = utils.next_file_name('./logs/' + log_name, 'log')
        logging.basicConfig(
        	level=logging.INFO,
        	format='%(levelname)s:%(asctime)s:%(module)s:%(message)s',
            datefmt=self.config['info']['timestamp_format'],
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()
            ])
        log.info('Logging started.')
        log.info('Initializing microscope object using file {}.'.format(config_file))

        self.ureg = ureg
        # Callable for converting a string into a quantity with units
        self.Q_ = ureg.Quantity
        self.temp = temp

        self._add_atto()
        self._add_scanner()
        self._add_SQUID()
        self._add_lockins()
        self._add_preamps()

    def _add_atto(self):
        """Add Attocube controller to microscope.
        """
        atto_config = self.config['instruments']['atto']
        ts_fmt = self.config['info']['timestamp_format']
        if hasattr(self, 'atto'):
            self.atto.clear_instances()
        self.remove_component(atto_config['name'])
        self.atto = atto.ANC300(atto_config, self.temp, self.ureg, ts_fmt)
        self.add_component(self.atto)
        log.info('Attocube controller successfully added to microscope.')

    def _add_scanner(self):
        """Add scanner instrument to microscope.
        """
        scanner_config = self.config['instruments']['scanner']
        daq_config = self.config['instruments']['daq']
        if hasattr(self, 'scanner'):
            self.scanner.clear_instances()
        self.remove_component(scanner_config['name'])
        self.scanner = Scanner(scanner_config, daq_config, self.temp, self.ureg)
        self.add_component(self.scanner)
        log.info('Scanner successfully added to microscope.')
    
    def _add_SQUID(self):
        """Add SQUID instrument to microscope.
        """
        squid_config = self.config['SQUID']
        if hasattr(self, 'SQUID'):
            self.SQUID.clear_instances()
        self.remove_component(squid_config['name'])
        squid_type = squid_config['type'].lower().capitalize()
        self.SQUID = getattr(sys.modules['squids'], squid_type)(squid_config)
        self.add_component(self.SQUID)
        log.info('{}(SQUID) successfully added to microscope.'.format(squid_type))
        
    def _add_lockins(self):
        """Add lockins to microscope.
        """
        for lockin, lockin_info in self.config['instruments']['lockins'].items():
            name = '{}_lockin'.format(lockin)
            address = lockin_info['address']
            if hasattr(self, name):
                getattr(self, name, 'clear_instances')()
            self.remove_component(name)
            instr = SR830(name, address, metadata={lockin: lockin_info})
            setattr(self, name, instr)
            self.add_component(getattr(self, '{}_lockin'.format(lockin)))
            log.info('{} successfully added to microscope.'.format(name))
            
    def _add_preamps(self) -> None:
        """Add preamps (e.g. SR560) to microscope.

        .. TODO:: Add SR560 preamps.
        """
        pass
            
    def set_lockins(self, measurement: Dict[str, Any]) -> None:
        """Initialize lockins for given measurement.

        Args:
            measurement: Dict of measurement parameters as defined
                in measurement configuration file.
        """
        channels = measurement['channels']
        for ch in channels.keys():
            if 'lockin' in channels[ch].keys():
                lockin = '{}_lockin'.format(channels[ch]['lockin']['name'])
                for param in channels[ch]['lockin'].keys():
                    if param != 'name':
                        parameters = getattr(self, lockin).parameters
                        unit = parameters[param].unit
                        value = self.Q_(channels[ch]['lockin'][param]).to(unit).magnitude
                        log.info('Setting {} on {} to {} {}.'.format(param, lockin, value, unit))
                        parameters[param].set(value)
    
    def goto(self, new_position: List[Union[float, int]]) -> None:
        """Alias for self.scanner.position.set() or equivalently self.scanner.goto().

        Args:
            new_position: List of [x, y, z] voltages to apply to benders.
        """
        self.scanner.position(new_position)

    def tdCAP(self, tdc_params: Dict[str, Any], getting_plane: bool=False) -> None:
        """Capacitive touchdown.

        This is a work in progress!

        Args:
            tdc_params: Dict of capacitive touchdown parameters as defined
                in measurement configuration file.
            getting_plane: Whether tdCAP() is being called in a loop to acquire a plane.
        """
        old_pos = self.scanner.position()
        constants = tdc_params['constants']
        ai_channels = daq_config['channels']['analog_inputs']
        meas_channels = tdc_params['channels']
        channels = {} 
        for ch, _ in meas_channels.items():
            channels.update({ch: ai_channels[ch]})
        nchannels = len(channels.keys())
        daq_config = self.config['isntruments']['daq']
        daq_name = daq_config['name']
        daq_rate = self.Q_(daq_config['rate']).to('Hz').magnitude / nchannels
        self.set_lockins(tdc_params)
        dV = self.Q_(tdc_params['dV']).to('V').magnitude
        startV, endV = [self.Q_(lim).to('V').magnitude for lim in tdc_params['range']]
        npts = int((tdc_params['range'][1] - tdc_params['range'][0]) / dV)
        heights = np.linspace(startV, endV, npnts)
        caps = np.empty_like(heights)
        delay = constants['wait_factor'] * self.CAP_lockin.time_constant()
        with nidaqmx.Task('tdcap_ai_task') as ai_task:
            self.remove_component('daq_ai')
            if hasattr(self, 'daq_ai'):
                self.daq_ai.clear_instances()
            self.daq_ai = DAQAnalogInputs('daq_ai',
                                          daq_name,
                                          daq_rate,
                                          channels,
                                          ai_task
                                         )
            loop = qc.Loop(self.scanner.height.sweep(startV, endV, dV), delay=delay
                ).each(
                    self.daq_ai.voltage,
                    self.daq_ai.CAP,
                    qc.Task(utils.check_for_td)
                ).then()
            data = loop.get_data_set(name=tdc_params['fname'])
            try:
                pass
            except KeyboardInterrupt:
                log.warning('Scan interrupted by user. Going back to {}.'.format(old_pos))
                self.goto(old_pos)
        self.goto(old_pos)
        return data
 
    def remove_component(self, name: str) -> None:
        """Remove a component (instrument) from the microscope.

        Args:
            name: Name of component to remove.
        """
        if name in self.components.keys():
            _ = self.components.pop(name)
            log.info('Removed {} from microscope.'.format(name))
        else:
            log.debug('Microscope has no component with the name {}'.format(name))  
                
class SusceptometerMicroscope(Microscope):
    """Scanning SQUID susceptometer microscope class.
    """
    def __init__(self, config_file: str, temp: str, ureg: Any=ureg, log_level: Any=logging.INFO,
                 log_name: str=None, **kwargs) -> None:
        """
        Args:
            config_file: Path to microscope configuration JSON file.
            temp: 'LT' or 'RT', depending on whether the microscope is cold or not.
                Sets the voltage limits for the scanner and Attocubes.
            ureg: pint UnitRegistry for managing physical units.
            log_level: e.g. logging.DEBUG or logging.INFO
            log_name: Log file will be saved as logs/{log_name}.log.
                Default is the name of the microscope configuration file.
            **kwargs: Keyword arguments to be passed to Station constructor.
        """
        super().__init__(config_file, temp, ureg, log_level, log_name, **kwargs)

    def scan_plane(self, scan_params: Dict[str, Any]) -> Any:
        """
        Scan the current plane while acquiring data in the channels defined in
        measurement configuration file (e.g. MAG, SUSCX, SUSCY, CAP).

        Args:
            scan_params: Dict of scan parameters as defined
                in measuremnt configuration file.

        Returns:
            Tuple[DataSet, Scanplot]: data, plot
                qcodes DataSet containing acquired arrays and metdata,
                and ScanPlot instance populated with acquired data.
        """
        if not self.scanner.metadata['position']['plane_is_current']:
            raise RuntimeError('Plane is not current. Aborting scan.')
        old_pos = self.scanner.position()
        
        daq_config = self.config['instruments']['daq']
        ao_channels = daq_config['channels']['analog_outputs']
        ai_channels = daq_config['channels']['analog_inputs']
        meas_channels = scan_params['channels']
        channels = {}
        for ch in meas_channels.keys():
            channels.update({ch: ai_channels[ch]})
        nchannels = len(channels.keys())
        
        daq_name = daq_config['name']
        #: DAQ AI sampling rate is divided amongst all active AI channels
        daq_rate = self.Q_(daq_config['rate']).to('Hz').magnitude / nchannels
        
        fast_ax = scan_params['fast_ax'].lower()
        slow_ax = 'x' if fast_ax == 'y' else 'y'
        
        pix_per_line = scan_params['scan_size'][fast_ax]
        line_duration = pix_per_line * self.Q_('pixels') / self.Q_(scan_params['scan_rate'])
        pts_per_line = int(daq_rate * line_duration.to('s').magnitude)
        
        plane = self.scanner.metadata['position']['plane']
        height = self.Q_(scan_params['height']).to('V').magnitude
        
        scan_vectors = utils.make_scan_vectors(scan_params, self.ureg)
        scan_grids = utils.make_scan_grids(scan_vectors, slow_ax, fast_ax,
                                           pts_per_line, plane, height)
        utils.validate_scan_params(self.scanner.metadata, scan_params,
                                   scan_grids, self.temp, self.ureg, log)
        self.goto([scan_grids[axis][0][0] for axis in ['x', 'y', 'z']])
        self.set_lockins(scan_params)
        #: get channel prefactors in pint Quantity form
        prefactors = self.get_prefactors(scan_params)
        #: get channel prefactors in string form so they can be saved in metadata
        prefactor_strs = {}
        for ch, prefac in prefactors.items():
            prefactor_strs.update({ch: '{} {}'.format(prefac.magnitude, prefac.units)})
        fast_ax_vec = qc.DataArray(name='position_{}'.format(fast_ax),
                                   label='{} position'.format(fast_ax),
                                   array_id='benders_position_{}_set'.format(fast_ax),
                                   unit='V',
                                   is_setpoint=True,
                                   preset_data=scan_vectors[fast_ax]
                                   )
        slow_ax_vec = qc.DataArray(name='position_{}'.format(slow_ax),
                                   label='{} position'.format(slow_ax),
                                   array_id='benders_position_{}'.format(slow_ax),
                                   unit='V',
                                   is_setpoint=True,
                                   preset_data=scan_vectors[slow_ax]
                                   )
        with nidaqmx.Task('scan_plane_ai_task') as ai_task:
            self.remove_component('daq_ai')
            if hasattr(self, 'daq_ai'):
                self.daq_ai.clear_instances()
            self.daq_ai = DAQAnalogInputs('daq_ai',
                                          daq_name,
                                          daq_rate,
                                          channels,
                                          ai_task,
                                          samples_to_read=pts_per_line,
                                          target_points=pix_per_line,
                                          setpoints=(fast_ax_vec,),
                                          #: Very important to synchronize AOs and AIs
                                          clock_src='ao/SampleClock'
                                         )
            self.add_component(self.daq_ai)
            slow_ax_position = getattr(self.scanner, 'position_{}'.format(slow_ax))
            slow_ax_start = scan_vectors[slow_ax][0]
            slow_ax_end = scan_vectors[slow_ax][-1]
            slow_ax_step = scan_vectors[slow_ax][1] - scan_vectors[slow_ax][0]
            #: There is probably a counter built in to qc.Loop, but I couldn't find it
            loop_counter = utils.Counter()
            scan_plot = ScanPlot(scan_params, prefactors, self.ureg)
            loop = qc.Loop(slow_ax_position.sweep(start=slow_ax_start,
                                                  stop=slow_ax_end,
                                                  step=slow_ax_step), delay=0.1
            ).each(
                #: Create AO task and queue data to be written to AOs
                qc.Task(self.scanner.scan_line, scan_grids, ao_channels, daq_rate, loop_counter),
                #: Start AI task; acquisition won't start until AO task is started
                qc.Task(ai_task.start),
                #: Start AO task
                qc.Task(self.scanner.control_ao_task, 'start'),
                #: Acquire voltage from all active AI channels
                self.daq_ai.voltage,
                qc.Task(ai_task.wait_until_done),
                qc.Task(self.scanner.control_ao_task, 'wait_until_done'),
                qc.Task(ai_task.stop),
                #: Stop and close AO task so that AOs can be used for goto
                qc.Task(self.scanner.control_ao_task, 'stop'),
                qc.Task(self.scanner.control_ao_task, 'close'),
                qc.Task(self.scanner.goto_start_of_next_line, scan_grids, loop_counter),
                #: Update and save plot
                qc.Task(scan_plot.update, qc.loops.active_data_set, loop_counter),
                qc.Task(scan_plot.save),
                qc.Task(loop_counter.advance)
            ).then(
                qc.Task(self.daq_ai.clear_instances),
                qc.Task(self.goto, old_pos),
                qc.Task(self.CAP_lockin.amplitude, 0.004),
                qc.Task(self.SUSC_lockin.amplitude, 0.004)
            )
            #: loop.metadata will be saved in DataSet
            loop.metadata.update(scan_params)
            loop.metadata.update({'prefactors': prefactor_strs})
            for ch, idx in channels.items():
                loop.metadata['channels'][ch].update({'ai': idx})
            data = loop.get_data_set(name=scan_params['fname'])
            #: Run the loop
            try:
                loop.run()
                log.info('Scan completed. DataSet saved to {}.'.format(data.location))
            #: If loop is aborted by user:
            except KeyboardInterrupt:
                log.warning('Scan interrupted by user. Going to {}.'.format(old_pos))
                #: If there's an active AO task, close it so that we can use goto
                if nidaqmx.system.System.local().tasks.task_names:
                    self.scanner.control_ao_task('stop')
                    self.scanner.control_ao_task('close')
                self.goto(old_pos)
                self.CAP_lockin.amplitude(0.004)
                self.SUSC_lockin.amplitude(0.004)
                log.info('Scan aborted by user. DataSet saved to {}.'.format(data.location))
        self.remove_component('daq_ai')
        return data, scan_plot

    def get_prefactors(self, measurement: Dict[str, Any], update: bool=True) -> Dict[str, Any]:
        """For each channel, calculate prefactors to convert DAQ voltage into real units.

        Args:
            measurement: Dict of measurement parameters as defined
                in measurement configuration file.
            update: Whether to query instrument parameters or simply trust the
                latest values (should this even be an option)?

        Returns:
            Dict[str, Quantity]: prefactors
                Dict of {channel_name: prefactor} where prefactor is a pint Quantity.

        .. TODO:: Add current imaging channel.
        """
        channels = measurement['channels'].keys()
        mod_width = self.Q_(self.SQUID.metadata['modulation_width'])
        prefactors = {}
        for ch in channels:
            prefactor = 1
            if ch == 'MAG':
                prefactor /= mod_width
            elif ch in ['SUSCX', 'SUSCY']:
                r_lead = self.Q_(measurement['channels'][ch]['r_lead'])
                snap = getattr(self, 'SUSC_lockin').snapshot(update=update)['parameters']
                sens = snap['sensitivity']['value']
                amp = snap['amplitude']['value'] * self.ureg(snap['amplitude']['unit'])
                #: The factor of 10 here is because SR830 output gain is 10/sensitivity
                prefactor *=  (r_lead / amp) / (mod_width * 10 / sensitivity)
            if ch == 'CAP':
                snap = getattr(self, 'CAP_lockin').snapshot(update)['parameters']
                sens = snap['sensitivity']['value']
                #: The factor of 10 here is because SR830 output gain is 10/sensitivity
                prefactor /= (self.Q_(self.scanner.metadata['cantilever']['calibration']) * 10 / sensitivity)
            prefactor /= measurement['channels'][ch]['gain']
            prefactors.update({ch: prefactor})
        return prefactors

class SamplerMicroscope(Microscope):
    """Scanning SQUID sampler microscope class.
    """
    def __init__(self, config_file: str, temp: str, ureg: Any=ureg, log_level: Any=logging.INFO,
                 log_name: str=None, **kwargs) -> None:
        super().__init__(config_file, temp, ureg, log_level, log_name, **kwargs)

class DispersiveMicroscope(Microscope):
    """Scanning dispersive SQUID microscope class.
    """
    def __init__(self, config_file: str, temp: str, ureg: Any=ureg, log_level: Any=logging.INFO,
                 log_name: str=None, **kwargs) -> None:
        super().__init__(config_file, temp, ureg, log_level, log_name, **kwargs)