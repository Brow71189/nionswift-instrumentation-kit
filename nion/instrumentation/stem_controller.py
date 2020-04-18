# standard libraries
import abc
import asyncio
import enum
import functools
import gettext
import math
import threading
import typing

# third party libraries
# None

# local libraries
from nion.swift.model import DocumentModel
from nion.swift.model import Graphics
from nion.swift.model import HardwareSource
from nion.utils import Event
from nion.utils import Geometry
from nion.utils import Model
from nion.utils import Observable
from nion.utils import Registry

if typing.TYPE_CHECKING:
    from nion.swift.model import DataItem
    from nion.swift.model import DisplayItem


_ = gettext.gettext


class PMTType(enum.Enum):
    DF = 0
    BF = 1


class SubscanState(enum.Enum):
    INVALID = -1
    DISABLED = 0
    ENABLED = 1


AxisType = typing.Tuple[str, str]


class ScanContext:
    def __init__(self):
        self.center_nm = None
        self.fov_size_nm = None
        self.rotation_rad = None

    def __repr__(self) -> str:
        return f"{self.fov_size_nm[0]}nm {math.degrees(self.rotation_rad)}deg" if self.fov_size_nm else "NO CONTEXT"

    def __eq__(self, other) -> bool:
        return other is not None and isinstance(other, self.__class__) and other.center_nm == self.center_nm and other.fov_size_nm == self.fov_size_nm and other.rotation_rad == self.rotation_rad

    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        result.center_nm = self.center_nm
        result.fov_size_nm = self.fov_size_nm
        result.rotation_rad = self.rotation_rad
        return result

    @property
    def is_valid(self) -> bool:
        return self.fov_size_nm is not None

    def clear(self) -> None:
        self.center_nm = None
        self.fov_size_nm = None
        self.rotation_rad = None

    def update(self, center_nm: Geometry.FloatPoint, fov_size_nm: Geometry.FloatSize, rotation_rad: float) -> None:
        self.center_nm = Geometry.FloatPoint.make(center_nm)
        self.fov_size_nm = Geometry.FloatSize.make(fov_size_nm)
        self.rotation_rad = rotation_rad


class STEMController:
    """An interface to a STEM microscope.

    Methods and properties starting with a single underscore are called internally and shouldn't be called by general
    clients.

    Methods starting with double underscores are private.

    Probe
    -----
    probe_state (parked, blanked, scanning)
    probe_position (fractional coordinates, optional)
    set_probe_position(probe_position)
    validate_probe_position()

    probe_state_changed_event (probe_state, probe_position)
    """

    def __init__(self):
        self.__probe_position_value : Model.PropertyModel[Geometry.FloatPoint] = Model.PropertyModel()
        self.__probe_position_value.on_value_changed = self.set_probe_position
        self.__probe_state_stack = list()  # parked, or scanning
        self.__probe_state_stack.append("parked")
        self.__scan_context = ScanContext()
        self.probe_state_changed_event = Event.Event()
        self.__subscan_state_value : Model.PropertyModel[SubscanState] = Model.PropertyModel(SubscanState.INVALID)
        self.__subscan_region_value = Model.PropertyModel(None)
        self.__subscan_rotation_value = Model.PropertyModel(0.0)
        self.__scan_context_data_items : typing.List["DataItem.DataItem"] = list()
        self.scan_context_data_items_changed_event = Event.Event()
        self.__ronchigram_camera = None
        self.__eels_camera = None
        self.__scan_controller = None

    def close(self):
        self.__scan_context_data_items = None
        self.__subscan_state_value.close()
        self.__subscan_state_value = None
        self.__subscan_region_value.close()
        self.__subscan_region_value = None
        self.__subscan_rotation_value.close()
        self.__subscan_rotation_value = None
        self.__probe_position_value.close()
        self.__probe_position_value = None

    # configuration methods

    @property
    def ronchigram_camera(self) -> HardwareSource.HardwareSource:
        if self.__ronchigram_camera:
            return self.__ronchigram_camera
        return Registry.get_component("ronchigram_camera_hardware_source")

    def set_ronchigram_camera(self, camera: HardwareSource.HardwareSource) -> None:
        assert camera.features.get("is_ronchigram_camera", False)
        self.__ronchigram_camera = camera

    @property
    def eels_camera(self) -> HardwareSource.HardwareSource:
        if self.__eels_camera:
            return self.__eels_camera
        return Registry.get_component("eels_camera_hardware_source")

    def set_eels_camera(self, camera: HardwareSource.HardwareSource) -> None:
        assert camera.features.get("is_eels_camera", False)
        self.__eels_camera = camera

    @property
    def scan_controller(self) -> HardwareSource.HardwareSource:
        if self.__scan_controller:
            return self.__scan_controller
        return Registry.get_component("scan_hardware_source")

    def set_scan_controller(self, scan_controller: HardwareSource.HardwareSource) -> None:
        self.__scan_controller = scan_controller

    # end configuration methods

    def _enter_scanning_state(self) -> None:
        # push 'scanning' onto the probe state stack; the `probe_state` will now be `scanning`
        self.__probe_state_stack.append("scanning")
        # fire off the probe state changed event.
        self.probe_state_changed_event.fire(self.probe_state, self.probe_position)
        # ensure that SubscanState is valid (ENABLED or DISABLED, not INVALID)
        if self._subscan_state_value.value == SubscanState.INVALID:
            self._subscan_state_value.value = SubscanState.DISABLED

    def _exit_scanning_state(self) -> None:
        # pop the 'scanning' probe state and fire off the probe state changed event.
        self.__probe_state_stack.pop()
        self.probe_state_changed_event.fire(self.probe_state, self.probe_position)

    def _enter_synchronized_state(self, scan_controller: HardwareSource.HardwareSource, *, camera: HardwareSource.HardwareSource=None) -> None:
        pass

    def _exit_synchronized_state(self, scan_controller: HardwareSource.HardwareSource, *, camera: HardwareSource.HardwareSource=None) -> None:
        pass

    @property
    def _probe_position_value(self) -> Model.PropertyModel[Geometry.FloatPoint]:
        """Internal use."""
        return self.__probe_position_value

    @property
    def _subscan_state_value(self) -> Model.PropertyModel[SubscanState]:
        """Internal use."""
        return self.__subscan_state_value

    @property
    def subscan_state(self) -> SubscanState:
        return typing.cast(SubscanState, self.__subscan_state_value.value)

    @subscan_state.setter
    def subscan_state(self, value: SubscanState) -> None:
        self.__subscan_state_value.value = value

    @property
    def _subscan_region_value(self):
        """Internal use."""
        return self.__subscan_region_value

    @property
    def subscan_region(self) -> typing.Optional[Geometry.FloatRect]:
        region_tuple = self.__subscan_region_value.value
        return Geometry.FloatRect.make(region_tuple) if region_tuple is not None else None

    @subscan_region.setter
    def subscan_region(self, value: typing.Optional[Geometry.FloatRect]) -> None:
        self.__subscan_region_value.value = tuple(value) if value is not None else None

    @property
    def _subscan_rotation_value(self):
        """Internal use."""
        return self.__subscan_rotation_value

    @property
    def subscan_rotation(self) -> float:
        return self.__subscan_rotation_value.value

    @subscan_rotation.setter
    def subscan_rotation(self, value: float):
        self.__subscan_rotation_value.value = value

    def disconnect_probe_connections(self):
        self.__scan_context_data_items = list()
        self.scan_context_data_items_changed_event.fire()

    def _data_item_states_changed(self, data_item_states):
        if len(data_item_states) > 0:
            if self.subscan_state == SubscanState.DISABLED:
                # only update context display items when subscan is disabled
                self.__scan_context_data_items = [data_item_state.get("data_item") for data_item_state in data_item_states]
            self.scan_context_data_items_changed_event.fire()

    @property
    def scan_context_data_items(self) -> typing.Sequence["DataItem.DataItem"]:
        return self.__scan_context_data_items

    @property
    def scan_context(self) -> ScanContext:
        return self.__scan_context

    def _update_scan_context(self, center_nm: Geometry.FloatPoint, fov_size_nm: Geometry.FloatSize, rotation_rad: float) -> None:
        self.__scan_context.update(center_nm, fov_size_nm, rotation_rad)

    def _clear_scan_context(self) -> None:
        self.__scan_context.clear()

    @property
    def probe_position(self) -> typing.Optional[Geometry.FloatPoint]:
        """ Return the probe position, in normalized coordinates with origin at top left. Only valid if probe_state is 'parked'."""
        return self.__probe_position_value.value

    @probe_position.setter
    def probe_position(self, value: typing.Optional[Geometry.FloatPoint]) -> None:
        self.set_probe_position(value)

    def set_probe_position(self, new_probe_position: typing.Optional[Geometry.FloatPoint]) -> None:
        """ Set the probe position, in normalized coordinates with origin at top left. """
        if new_probe_position is not None:
            # convert the probe position to a FloatPoint and limit it to the 0.0 to 1.0 range in both axes.
            new_probe_position = Geometry.FloatPoint(y=max(min(new_probe_position.y, 1.0), 0.0),
                                                     x=max(min(new_probe_position.x, 1.0), 0.0))
        old_probe_position = self.__probe_position_value.value
        if ((old_probe_position is None) != (new_probe_position is None)) or (old_probe_position != new_probe_position):
            # this path is only taken if set_probe_position is not called as a result of the probe_position model
            # value changing.
            self.__probe_position_value.value = new_probe_position
        # update the probe position for listeners and also explicitly update for probe_graphic_connections.
        self.probe_state_changed_event.fire(self.probe_state, self.probe_position)

    def validate_probe_position(self):
        """Validate the probe position.

        This is called when the user switches from not controlling to controlling the position."""
        self.set_probe_position(Geometry.FloatPoint(y=0.5, x=0.5))

    @property
    def probe_state(self) -> str:
        """Probe state is the current probe state and can be 'parked', or 'scanning'."""
        return self.__probe_state_stack[-1]

    # instrument API

    def set_control_output(self, name, value, options=None):
        options = options if options else dict()
        value_type = options.get("value_type", "output")
        inform = options.get("inform", False)
        confirm = options.get("confirm", False)
        confirm_tolerance_factor = options.get("confirm_tolerance_factor", 1.0)  # instrument keeps track of default; this is a factor applied to the default
        confirm_timeout = options.get("confirm_timeout", 16.0)
        if value_type == "output":
            if inform:
                self.InformControl(name, value)
            elif confirm:
                if not self.SetValAndConfirm(name, value, confirm_tolerance_factor, int(confirm_timeout * 1000)):
                    raise TimeoutError("Setting '" + name + "'.")
            else:
                self.SetVal(name, value)
        elif value_type == "delta" and not inform:
            self.SetValDelta(name, value)
        else:
            raise NotImplemented()

    def get_control_output(self, name):
        return self.GetVal(name)

    def get_control_state(self, name):
        value_exists, value = self.TryGetVal(name)
        return "unknown" if value_exists else None

    def get_property(self, name):
        if name in ("probe_position", "probe_state"):
            return getattr(self, name)
        return self.get_control_output(name)

    def set_property(self, name, value):
        if name in ("probe_position"):
            return setattr(self, name, value)
        return self.set_control_output(name, value)

    def apply_metadata_groups(self, properties: typing.MutableMapping, metatdata_groups: typing.Sequence[typing.Tuple[typing.Sequence[str], str]]) -> None:
        """Apply metadata groups to properties.

        Metadata groups is a tuple with two elements. The first is a list of strings representing a dict-path in which
        to add the controls. The second is a control group from which to read a list of controls to be added as name
        value pairs to the dict-path.
        """
        pass

    # end instrument API

    # required functions (templates). subclasses should override.

    def TryGetVal(self, s: str) -> typing.Tuple[bool, typing.Optional[float]]:
        return False, None

    def GetVal(self, s: str, default_value: float=None) -> float:
        raise Exception(f"No element named '{s}' exists! Cannot get value.")

    def SetVal(self, s: str, val: float) -> bool:
        return False

    def SetValWait(self, s: str, val: float, timeout_ms: int) -> bool:
        return False

    def SetValAndConfirm(self, s: str, val: float, tolfactor: float, timeout_ms: int) -> bool:
        return False

    def SetValDelta(self, s: str, delta: float) -> bool:
        return False

    def SetValDeltaAndConfirm(self, s: str, delta: float, tolfactor: float, timeout_ms: int) -> bool:
        return False

    def InformControl(self, s: str, val: float) -> bool:
        return False

    def GetVal2D(self, s:str, default_value: Geometry.FloatPoint=None, *, axis: AxisType) -> Geometry.FloatPoint:
        raise Exception(f"No 2D element named '{s}' exists! Cannot get value.")

    def SetVal2D(self, s:str, value: Geometry.FloatPoint, *, axis: AxisType) -> bool:
        return False

    def SetVal2DAndConfirm(self, s: str, val: Geometry.FloatPoint, tolfactor: float, timeout_ms: int, *, axis: AxisType) -> bool:
        return False

    def SetVal2DDelta(self, s: str, delta: Geometry.FloatPoint, *, axis: AxisType) -> bool:
        return False

    def SetVal2DDeltaAndConfirm(self, s: str, delta: Geometry.FloatPoint, tolfactor: float, timeout_ms: int, *, axis: AxisType) -> bool:
        return False

    def InformControl2D(self, s: str, val: Geometry.FloatPoint, *, axis: AxisType) -> bool:
        return False

    # end required functions

    # high level commands

    def change_stage_position(self, *, dy: int=None, dx: int=None):
        """Shift the stage by dx, dy (meters). Do not wait for confirmation."""
        raise NotImplemented()

    def change_pmt_gain(self, pmt_type: PMTType, *, factor: float) -> None:
        """Change specified PMT by factor. Do not wait for confirmation."""
        raise NotImplemented()

    # end high level commands


class AbstractGraphicSetHandler(abc.ABC):
    """Handle callbacks from the graphic set controller to the model."""

    @abc.abstractmethod
    def _create_graphic(self) -> Graphics.Graphic:
        """Called to create a new graphic for a new display item."""
        ...

    @abc.abstractmethod
    def _update_graphic(self, graphic: Graphics.Graphic) -> None:
        """Called to update the graphic when the model changes."""
        ...

    @abc.abstractmethod
    def _graphic_property_changed(self, graphic: Graphics.Graphic, name: str) -> None:
        """Called to update the model when the graphic changes."""
        ...

    @abc.abstractmethod
    def _graphic_removed(self, graphic: Graphics.Graphic) -> None:
        """Called when one of the graphics are removed."""
        ...


class GraphicSetController:

    def __init__(self, handler: AbstractGraphicSetHandler):
        self.__graphic_trackers : typing.List[typing.Tuple[Graphics.Graphic, Event.EventListener, Event.EventListener, Event.EventListener]] = list()
        self.__handler = handler

    def close(self):
        for _, graphic_property_changed_listener, remove_region_graphic_event_listener, display_about_to_be_removed_listener in self.__graphic_trackers:
            graphic_property_changed_listener.close()
            remove_region_graphic_event_listener.close()
            display_about_to_be_removed_listener.close()
        self.__graphic_trackers = list()

    @property
    def graphics(self) -> typing.Sequence[Graphics.Graphic]:
        return [t[0] for t in self.__graphic_trackers]

    def synchronize_graphics(self, display_items: typing.Sequence["DisplayItem.DisplayItem"]) -> None:
        # create graphics for each scan data item if it doesn't exist
        if not self.__graphic_trackers:
            for display_item in display_items:
                graphic = self.__handler._create_graphic()

                graphic_property_changed_listener = graphic.property_changed_event.listen(functools.partial(self.__handler._graphic_property_changed, graphic))

                def graphic_removed(graphic: Graphics.Graphic) -> None:
                    self.__remove_one_graphic(graphic)
                    self.__handler._graphic_removed(graphic)

                def display_removed(graphic: Graphics.Graphic) -> None:
                    self.__remove_one_graphic(graphic)

                remove_region_graphic_event_listener = graphic.about_to_be_removed_event.listen(functools.partial(graphic_removed, graphic))
                display_about_to_be_removed_listener = display_item.about_to_be_removed_event.listen(functools.partial(display_removed, graphic))
                self.__graphic_trackers.append((graphic, graphic_property_changed_listener, remove_region_graphic_event_listener, display_about_to_be_removed_listener))
                display_item.add_graphic(graphic)
        # apply new value to any existing graphics
        for graphic in self.graphics:
            self.__handler._update_graphic(graphic)

    def remove_all_graphics(self) -> None:
        # remove any graphics
        for graphic, graphic_property_changed_listener, remove_region_graphic_event_listener, display_about_to_be_removed_listener in self.__graphic_trackers:
            graphic_property_changed_listener.close()
            remove_region_graphic_event_listener.close()
            display_about_to_be_removed_listener.close()
            graphic.container.remove_graphic(graphic)
        self.__graphic_trackers = list()

    def __remove_one_graphic(self, graphic_to_remove) -> None:
        graphic_trackers = list()
        for graphic, graphic_property_changed_listener, remove_region_graphic_event_listener, display_about_to_be_removed_listener in self.__graphic_trackers:
            if graphic_to_remove != graphic:
                graphic_trackers.append((graphic, graphic_property_changed_listener, remove_region_graphic_event_listener, display_about_to_be_removed_listener))
            else:
                graphic_property_changed_listener.close()
                remove_region_graphic_event_listener.close()
                display_about_to_be_removed_listener.close()
        self.__graphic_trackers = graphic_trackers


class DisplayItemListModel(Observable.Observable):
    """Make an observable list model from the item source with a list as the item."""

    def __init__(self, document_model: DocumentModel.DocumentModel, item_key: str,
                 predicate: typing.Callable[["DisplayItem.DisplayItem"], bool],
                 change_event: typing.Optional[Event.Event] = None):
        super().__init__()
        self.__document_model = document_model
        self.__item_key = item_key
        self.__predicate = predicate
        self.__items : typing.List["DisplayItem.DisplayItem"] = list()

        self.__item_inserted_listener = document_model.item_inserted_event.listen(self.__item_inserted)
        self.__item_removed_listener = document_model.item_removed_event.listen(self.__item_removed)

        for index, display_item in enumerate(document_model.display_items):
            self.__item_inserted("display_items", display_item, index)

        self.__change_event_listener = change_event.listen(self.refilter) if change_event else None

        # special handling when document closes
        def unlisten():
            if self.__change_event_listener:
                self.__change_event_listener.close()
                self.__change_event_listener = None

        self.__document_close_listener = document_model.about_to_close_event.listen(unlisten)

    def close(self) -> None:
        if self.__change_event_listener:
            self.__change_event_listener.close()
            self.__change_event_listener = None
        self.__item_inserted_listener.close()
        self.__item_inserted_listener = None
        self.__item_removed_listener.close()
        self.__item_removed_listener = None
        self.__document_close_listener.close()
        self.__document_close_listener = None
        self.__document_model = None

    def __item_inserted(self, key: str, display_item: "DisplayItem.DisplayItem", index: int) -> None:
        if key == "display_items" and not display_item in self.__items and self.__predicate(display_item):
            index = len(self.__items)
            self.__items.append(display_item)
            self.notify_insert_item(self.__item_key, display_item, index)

    def __item_removed(self, key: str, display_item: "DisplayItem.DisplayItem", index: int) -> None:
        if key == "display_items" and display_item in self.__items:
            index = self.__items.index(display_item)
            self.__items.pop(index)
            self.notify_remove_item(self.__item_key, display_item, index)

    @property
    def items(self) -> typing.Sequence["DisplayItem.DisplayItem"]:
        return self.__items

    def __getattr__(self, item):
        if item == self.__item_key:
            return self.items
        raise AttributeError()

    def refilter(self) -> None:
        self.item_set = set(self.__items)
        for display_item in self.__document_model.display_items:
            if self.__predicate(display_item):
                # insert item if not already inserted
                if not display_item in self.__items:
                    index = len(self.__items)
                    self.__items.append(display_item)
                    self.notify_insert_item(self.__item_key, display_item, index)
            else:
                # remove item if in list
                if display_item in self.__items:
                    index = self.__items.index(display_item)
                    self.__items.pop(index)
                    self.notify_remove_item(self.__item_key, display_item, index)


def ScanContextDisplayItemListModel(document_model: DocumentModel.DocumentModel, stem_controller: STEMController) -> DisplayItemListModel:
    def is_scan_context_display_item(display_item: "DisplayItem.DisplayItem") -> bool:
        return display_item.data_item in stem_controller.scan_context_data_items

    return DisplayItemListModel(document_model, "display_items", is_scan_context_display_item, stem_controller.scan_context_data_items_changed_event)


class EventLoopMonitor:
    """Utility base class to monitor availability of event loop."""

    def __init__(self, document_model: DocumentModel.DocumentModel, event_loop: asyncio.AbstractEventLoop):
        self.__event_loop : typing.Optional[asyncio.AbstractEventLoop] = event_loop
        self.__document_close_listener = document_model.about_to_close_event.listen(self._unlisten)
        self.__closed = False

    def _unlisten(self) -> None:
        pass

    def _mark_closed(self) -> None:
        self.__closed = True
        self.__document_close_listener.close()
        self.__document_close_listener = None
        self.__event_loop = None

    def _call_soon_threadsafe(self, fn: typing.Callable, *args) -> None:
        if not self.__closed:
            def safe_fn() -> None:
                if not self.__closed:
                    fn(*args)

            assert self.__event_loop
            self.__event_loop.call_soon_threadsafe(safe_fn)


class ProbeView(EventLoopMonitor, AbstractGraphicSetHandler, DocumentModel.AbstractImplicitDependency):
    """Observes the probe (STEM controller) and updates data items and graphics."""

    def __init__(self, stem_controller: STEMController, document_model: DocumentModel.DocumentModel, event_loop: asyncio.AbstractEventLoop):
        super().__init__(document_model, event_loop)
        self.__stem_controller = stem_controller
        self.__document_model = document_model
        self.__scan_display_items_model = ScanContextDisplayItemListModel(document_model, stem_controller)
        self.__graphic_set = GraphicSetController(self)
        # note: these property changed listeners can all possibly be fired from a thread.
        self.__probe_state = None
        self.__probe_position_value = stem_controller._probe_position_value
        self.__probe_state_changed_listener = stem_controller.probe_state_changed_event.listen(self.__probe_state_changed)
        self.__document_model.register_implicit_dependency(self)

    def close(self):
        self._mark_closed()
        if self.__probe_state_changed_listener:
            self.__probe_state_changed_listener.close()
            self.__probe_state_changed_listener = None
        self.__document_model.unregister_implicit_dependency(self)
        self.__graphic_set.close()
        self.__graphic_set = None
        self.__scan_display_items_model.close()
        self.__scan_display_items_model = None
        self.__document_model = None
        self.__stem_controller = None

    def _unlisten(self) -> None:
        if self.__probe_state_changed_listener:
            self.__probe_state_changed_listener.close()
            self.__probe_state_changed_listener = None

    def __probe_state_changed(self, probe_state: str, probe_position: typing.Optional[Geometry.FloatPoint]) -> None:
        # thread safe. move actual call to main thread using the event loop.
        self._call_soon_threadsafe(self.__update_probe_state, probe_state, probe_position)

    def __update_probe_state(self, probe_state: str, probe_position: typing.Optional[Geometry.FloatPoint]) -> None:
        assert threading.current_thread() == threading.main_thread()
        if probe_state != "scanning" and probe_position is not None:
            self.__graphic_set.synchronize_graphics(self.__scan_display_items_model.display_items)
        else:
            self.__graphic_set.remove_all_graphics()

    # implement methods for the graphic set handler

    def _graphic_removed(self, subscan_graphic: Graphics.Graphic) -> None:
        # clear probe state
        self.__probe_position_value.value = None

    def _create_graphic(self) -> Graphics.PointGraphic:
        graphic = Graphics.PointGraphic()
        graphic.graphic_id = "probe"
        graphic.label = _("Probe")
        graphic.position = self.__probe_position_value.value
        graphic.is_bounds_constrained = True
        graphic.color = "#F80"
        return graphic

    def _update_graphic(self, graphic: Graphics.Graphic) -> None:
        graphic.position = self.__probe_position_value.value

    def _graphic_property_changed(self, graphic: Graphics.Graphic, name: str) -> None:
        if name == "position":
            self.__probe_position_value.value = Geometry.FloatPoint.make(graphic.position)

    def get_dependents(self, item) -> typing.Sequence:
        graphics = self.__graphic_set.graphics
        if item in graphics:
            return list(set(graphics) - {item})
        return list()


class SubscanView(EventLoopMonitor, AbstractGraphicSetHandler, DocumentModel.AbstractImplicitDependency):
    """Observes the STEM controller and updates data items and graphics."""

    def __init__(self, stem_controller: STEMController, document_model: DocumentModel.DocumentModel, event_loop: asyncio.AbstractEventLoop):
        super().__init__(document_model, event_loop)
        self.__stem_controller = stem_controller
        self.__document_model = document_model
        self.__scan_display_items_model = ScanContextDisplayItemListModel(document_model, stem_controller)
        self.__graphic_set = GraphicSetController(self)
        # note: these property changed listeners can all possibly be fired from a thread.
        self.__subscan_region_changed_listener = stem_controller._subscan_region_value.property_changed_event.listen(self.__subscan_region_changed)
        self.__subscan_rotation_changed_listener = stem_controller._subscan_rotation_value.property_changed_event.listen(self.__subscan_rotation_changed)
        self.__document_model.register_implicit_dependency(self)

    def close(self):
        self._mark_closed()
        self.__document_model.unregister_implicit_dependency(self)
        self.__graphic_set.close()
        self.__graphic_set = None
        self.__scan_display_items_model.close()
        self.__scan_display_items_model = None
        self.__document_model = None
        self.__stem_controller = None

    def _unlisten(self) -> None:
        self.__subscan_region_changed_listener.close()
        self.__subscan_region_changed_listener = None
        self.__subscan_rotation_changed_listener.close()
        self.__subscan_rotation_changed_listener = None

    # methods for handling changes to the subscan region

    def __subscan_region_changed(self, name: str) -> None:
        # must be thread safe
        self._call_soon_threadsafe(self.__update_subscan_region)

    def __subscan_rotation_changed(self, name: str) -> None:
        # must be thread safe
        self._call_soon_threadsafe(self.__update_subscan_region)

    def __update_subscan_region(self) -> None:
        assert threading.current_thread() == threading.main_thread()
        if self.__stem_controller.subscan_region:
            self.__graphic_set.synchronize_graphics(self.__scan_display_items_model.display_items)
        else:
            self.__graphic_set.remove_all_graphics()

    # implement methods for the graphic set handler

    def _graphic_removed(self, subscan_graphic: Graphics.Graphic) -> None:
        # clear subscan state
        self.__stem_controller.subscan_state = SubscanState.DISABLED
        self.__stem_controller.subscan_region = None
        self.__stem_controller.subscan_rotation = 0

    def _create_graphic(self) -> Graphics.RectangleGraphic:
        subscan_graphic = Graphics.RectangleGraphic()
        subscan_graphic.graphic_id = "subscan"
        subscan_graphic.label = _("Subscan")
        subscan_graphic.bounds = tuple(typing.cast(Geometry.FloatRect, self.__stem_controller.subscan_region))
        subscan_graphic.rotation = self.__stem_controller.subscan_rotation
        subscan_graphic.is_bounds_constrained = True
        return subscan_graphic

    def _update_graphic(self, subscan_graphic: Graphics.Graphic) -> None:
        subscan_graphic.bounds = tuple(typing.cast(Geometry.FloatRect, self.__stem_controller.subscan_region))
        subscan_graphic.rotation = self.__stem_controller.subscan_rotation

    def _graphic_property_changed(self, subscan_graphic: Graphics.Graphic, name: str) -> None:
        if name == "bounds":
            self.__stem_controller.subscan_region = Geometry.FloatRect.make(subscan_graphic.bounds)
        if name == "rotation":
            self.__stem_controller.subscan_rotation = subscan_graphic.rotation

    def get_dependents(self, item) -> typing.Sequence:
        graphics = self.__graphic_set.graphics
        if item in graphics:
            return list(set(graphics) - {item})
        return list()


class ScanContextController:
    """Manage probe view, subscan, and drift area for each instrument (STEMController) that gets registered."""

    def __init__(self, document_model, event_loop):
        assert event_loop is not None
        self.__document_model = document_model
        self.__event_loop = event_loop
        # be sure to keep a reference or it will be closed immediately.
        self.__instrument_added_event_listener = None
        self.__instrument_removed_event_listener = None
        self.__instrument_added_event_listener = HardwareSource.HardwareSourceManager().instrument_added_event.listen(self.register_instrument)
        self.__instrument_removed_event_listener = HardwareSource.HardwareSourceManager().instrument_removed_event.listen(self.unregister_instrument)
        for instrument in HardwareSource.HardwareSourceManager().instruments:
            self.register_instrument(instrument)

    def close(self):
        # close will be called when the extension is unloaded. in turn, close any references so they get closed. this
        # is not strictly necessary since the references will be deleted naturally when this object is deleted.
        for instrument in HardwareSource.HardwareSourceManager().instruments:
            self.unregister_instrument(instrument)
        self.__instrument_added_event_listener.close()
        self.__instrument_added_event_listener = None
        self.__instrument_removed_event_listener.close()
        self.__instrument_removed_event_listener = None

    def register_instrument(self, instrument):
        # if this is a stem controller, add a probe view
        if hasattr(instrument, "_probe_position_value"):
            instrument._probe_view = ProbeView(instrument, self.__document_model, self.__event_loop)
        if hasattr(instrument, "_subscan_region_value"):
            instrument._subscan_view = SubscanView(instrument, self.__document_model, self.__event_loop)

    def unregister_instrument(self, instrument):
        if hasattr(instrument, "_probe_view"):
            instrument._probe_view.close()
            instrument._probe_view = None
        if hasattr(instrument, "_subscan_view"):
            instrument._subscan_view.close()
            instrument._subscan_view = None


# the plan is to migrate away from the hardware manager as a registration system.
# but keep this here until that migration is complete.

def component_registered(component, component_types):
    if "stem_controller" in component_types:
        HardwareSource.HardwareSourceManager().register_instrument(component.instrument_id, component)

def component_unregistered(component, component_types):
    if "stem_controller" in component_types:
        HardwareSource.HardwareSourceManager().unregister_instrument(component.instrument_id)

component_registered_listener = Registry.listen_component_registered_event(component_registered)
component_unregistered_listener = Registry.listen_component_unregistered_event(component_unregistered)

for component in Registry.get_components_by_type("stem_controller"):
    component_registered(component, {"stem_controller"})
