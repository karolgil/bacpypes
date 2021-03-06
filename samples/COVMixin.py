#!/usr/bin/env python

"""
This sample application shows how to extend the basic functionality of a device 
to support the ReadPropertyMultiple service.
"""

from collections import defaultdict

from bacpypes.debugging import bacpypes_debugging, DebugContents, ModuleLogger
from bacpypes.consolelogging import ConfigArgumentParser
from bacpypes.consolecmd import ConsoleCmd
from bacpypes.errors import ExecutionError

from bacpypes.core import run, enable_sleeping
from bacpypes.task import OneShotTask, TaskManager
from bacpypes.pdu import Address

from bacpypes.constructeddata import SequenceOf, Any
from bacpypes.basetypes import DeviceAddress, COVSubscription, PropertyValue, \
    Recipient, RecipientProcess, ObjectPropertyReference
from bacpypes.app import LocalDeviceObject, BIPSimpleApplication
from bacpypes.object import Object, Property, PropertyError, \
    get_object_class, register_object_type, \
    AccessDoorObject, AccessPointObject, \
    AnalogInputObject, AnalogOutputObject,  AnalogValueObject, \
    LargeAnalogValueObject, IntegerValueObject, PositiveIntegerValueObject, \
    LightingOutputObject, BinaryInputObject, BinaryOutputObject, \
    BinaryValueObject, LifeSafetyPointObject, LifeSafetyZoneObject, \
    MultiStateInputObject, MultiStateOutputObject, MultiStateValueObject, \
    OctetStringValueObject, CharacterStringValueObject, TimeValueObject, \
    DateTimeValueObject, DateValueObject, TimePatternValueObject, \
    DatePatternValueObject, DateTimePatternValueObject, \
    CredentialDataInputObject, LoadControlObject, LoopObject, \
    PulseConverterObject
from bacpypes.apdu import SubscribeCOVRequest, \
    ConfirmedCOVNotificationRequest, \
    UnconfirmedCOVNotificationRequest, \
    SimpleAckPDU, Error, RejectPDU, AbortPDU

# some debugging
_debug = 0
_log = ModuleLogger(globals())

# globals
_generic_criteria_classes = {}
_cov_increment_criteria_classes = {}

# test globals
test_application = None

#
#   SubscriptionList
#

@bacpypes_debugging
class SubscriptionList:

    def __init__(self):
        if _debug: SubscriptionList._debug("__init__")

        self.cov_subscriptions = []

    def append(self, cov):
        if _debug: SubscriptionList._debug("append %r", cov)

        self.cov_subscriptions.append(cov)

    def remove(self, cov):
        if _debug: SubscriptionList._debug("remove %r", cov)

        self.cov_subscriptions.remove(cov)

    def find(self, client_addr, proc_id, obj_id):
        if _debug: SubscriptionList._debug("find %r %r %r", client_addr, proc_id, obj_id)

        for cov in self.cov_subscriptions:
            all_equal = (cov.client_addr == client_addr) and \
                (cov.proc_id == proc_id) and \
                (cov.obj_id == obj_id)
            if _debug: SubscriptionList._debug("    - cov, all_equal: %r %r", cov, all_equal)

            if all_equal:
                return cov

        return None

    def __len__(self):
        if _debug: SubscriptionList._debug("__len__")

        return len(self.cov_subscriptions)

    def __iter__(self):
        if _debug: SubscriptionList._debug("__iter__")

        for cov in self.cov_subscriptions:
            yield cov


#
#   Subscription
#

@bacpypes_debugging
class Subscription(OneShotTask, DebugContents):

    _debug_contents = (
        'obj_ref',
        'client_addr',
        'proc_id',
        'obj_id',
        'confirmed',
        'lifetime',
        )

    def __init__(self, obj_ref, client_addr, proc_id, obj_id, confirmed, lifetime):
        if _debug: Subscription._debug("__init__ %r %r %r %r %r %r", obj_ref, client_addr, proc_id, obj_id, confirmed, lifetime)
        OneShotTask.__init__(self)

        # save the reference to the related object
        self.obj_ref = obj_ref

        # save the parameters
        self.client_addr = client_addr
        self.proc_id = proc_id
        self.obj_id = obj_id
        self.confirmed = confirmed
        self.lifetime = lifetime

        # add ourselves to the subscription list for this object
        obj_ref._cov_subscriptions.append(self)

        # add ourselves to the list of all active subscriptions
        obj_ref._app.active_cov_subscriptions.append(self)

        # if lifetime is non-zero, schedule the subscription to expire
        if lifetime != 0:
            self.install_task(delta=self.lifetime)

    def cancel_subscription(self):
        if _debug: Subscription._debug("cancel_subscription")

        # suspend the task
        self.suspend_task()

        # remove ourselves from the other subscriptions for this object
        self.obj_ref._cov_subscriptions.remove(self)

        # remove ourselves from the list of all active subscriptions
        self.obj_ref._app.active_cov_subscriptions.remove(self)

        # break the object reference
        self.obj_ref = None

    def renew_subscription(self, lifetime):
        if _debug: Subscription._debug("renew_subscription")

        # suspend iff scheduled
        if self.isScheduled:
            self.suspend_task()

        # reschedule the task if its not infinite
        if lifetime != 0:
            self.install_task(delta=lifetime)

    def process_task(self):
        if _debug: Subscription._debug("process_task")

        # subscription is canceled
        self.cancel_subscription()

#
#   COVCriteria
#

@bacpypes_debugging
class COVCriteria:

    _properties_tracked = ()
    _properties_reported = ()
    _monitored_property_reference = None

    def _check_criteria(self):
        if _debug: COVCriteria._debug("_check_criteria")

        # assume nothing has changed
        something_changed = False

        # check all the things
        for property_name in self._properties_tracked:
            property_changed = (self._values[property_name] != self._cov_properties[property_name])
            if property_changed:
                if _debug: COVCriteria._debug("    - %s changed", property_name)

                # copy the new value for next time
                self._cov_properties[property_name] = self._values[property_name]

                something_changed = True

        if not something_changed:
            if _debug: COVCriteria._debug("    - nothing changed")

        # should send notifications
        return something_changed


@bacpypes_debugging
class GenericCriteria(COVCriteria):

    _properties_tracked = (
        'presentValue',
        'statusFlags',
        )
    _properties_reported = (
        'presentValue',
        'statusFlags',
        )
    _monitored_property_reference = 'presentValue'


@bacpypes_debugging
class COVIncrementCriteria(COVCriteria):

    _properties_tracked = (
        'presentValue',
        'statusFlags',
        )
    _properties_reported = (
        'presentValue',
        'statusFlags',
        )
    _monitored_property_reference = 'presentValue'

    def _check_criteria(self):
        if _debug: COVIncrementCriteria._debug("_check_criteria")

        # assume nothing has changed
        something_changed = False

        # get the old and new values
        old_present_value = self._cov_properties['presentValue']
        new_present_value = self._values['presentValue']
        cov_increment = self._values['covIncrement']

        # check the difference in values
        value_changed = (new_present_value <= (old_present_value - cov_increment)) \
            or (new_present_value >= (old_present_value + cov_increment))
        if value_changed:
            if _debug: COVIncrementCriteria._debug("    - present value changed")

            # copy the new value for next time
            self._cov_properties['presentValue'] = new_present_value

            something_changed = True

        # check the status flags
        status_changed = (self._values['statusFlags'] != self._cov_properties['statusFlags'])
        if status_changed:
            if _debug: COVIncrementCriteria._debug("    - status flags changed")

            # copy the new value for next time
            self._cov_properties['statusFlags'] = self._values['statusFlags']

            something_changed = True

        if not something_changed:
            if _debug: COVIncrementCriteria._debug("    - nothing changed")

        # should send notifications
        return something_changed

#
#   Change of Value Mixin
#

@bacpypes_debugging
class COVObjectMixin(object):

    _debug_contents = (
        '_cov_subscriptions',
        '_cov_properties',
        )

    def __init__(self, **kwargs):
        if _debug: COVObjectMixin._debug("__init__ %r", kwargs)
        super(COVObjectMixin, self).__init__(**kwargs)

        # list of all active subscriptions
        self._cov_subscriptions = SubscriptionList()

        # snapshot the properties tracked
        self._cov_properties = {}
        for property_name in self._properties_tracked:
            self._cov_properties[property_name] = self._values[property_name]

    def __setattr__(self, attr, value):
        if _debug: COVObjectMixin._debug("__setattr__ %r %r", attr, value)

        if attr.startswith('_') or attr[0].isupper() or (attr == 'debug_contents'):
            return object.__setattr__(self, attr, value)

        # use the default implementation
        super(COVObjectMixin, self).__setattr__(attr, value)

        # check for special properties
        if attr in self._properties_tracked:
            if _debug: COVObjectMixin._debug("    - property tracked")

            # check if it is significant
            if self._check_criteria():
                if _debug: COVObjectMixin._debug("    - send notifications")
                self._send_cov_notifications()
            else:
                if _debug: COVObjectMixin._debug("    - no notifications necessary")
        else:
            if _debug: COVObjectMixin._debug("    - property not tracked")

    def WriteProperty(self, propid, value, arrayIndex=None, priority=None, direct=False):
        if _debug: COVObjectMixin._debug("WriteProperty %r %r arrayIndex=%r priority=%r", propid, value, arrayIndex, priority)

        # normalize the property identifier
        if isinstance(propid, int):
            # get the property
            prop = self._properties.get(propid)
            if _debug: Object._debug("    - prop: %r", prop)

            if not prop:
                raise PropertyError(propid)

            # use the name from now on
            propid = prop.identifier
            if _debug: Object._debug("    - propid: %r", propid)

        # use the default implementation
        super(COVObjectMixin, self).WriteProperty(propid, value, arrayIndex, priority, direct)

        # check for special properties
        if propid in self._properties_tracked:
            if _debug: COVObjectMixin._debug("    - property tracked")

            # check if it is significant
            if self._check_criteria():
                if _debug: COVObjectMixin._debug("    - send notifications")
                self._send_cov_notifications()
            else:
                if _debug: COVObjectMixin._debug("    - no notifications necessary")
        else:
            if _debug: COVObjectMixin._debug("    - property not tracked")

    def _send_cov_notifications(self):
        if _debug: COVObjectMixin._debug("_send_cov_notifications")

        # check for subscriptions
        if not len(self._cov_subscriptions):
            return

        # get the current time from the task manager
        current_time = TaskManager().get_time()
        if _debug: COVObjectMixin._debug("    - current_time: %r", current_time)

        # create a list of values
        list_of_values = []
        for property_name in self._properties_reported:
            if _debug: COVObjectMixin._debug("    - property_name: %r", property_name)

            # get the class
            property_datatype = self.get_datatype(property_name)
            if _debug: COVObjectMixin._debug("        - property_datatype: %r", property_datatype)

            # build the value
            bundle_value = property_datatype(self._values[property_name])
            if _debug: COVObjectMixin._debug("        - bundle_value: %r", bundle_value)

            # bundle it into a sequence
            property_value = PropertyValue(
                propertyIdentifier=property_name,
                value=Any(bundle_value),
                )

            # add it to the list
            list_of_values.append(property_value)
        if _debug: COVObjectMixin._debug("    - list_of_values: %r", list_of_values)

        # loop through the subscriptions and send out notifications
        for cov in self._cov_subscriptions:
            if _debug: COVObjectMixin._debug("    - cov: %r", cov)

            # calculate time remaining
            if not cov.lifetime:
                time_remaining = 0
            else:
                time_remaining = int(cov.taskTime - current_time)

                # make sure it is at least one second
                if not time_remaining:
                    time_remaining = 1

            # build a request with the correct type
            if cov.confirmed:
                request = ConfirmedCOVNotificationRequest()
            else:
                request = UnconfirmedCOVNotificationRequest()

            # fill in the parameters
            request.pduDestination = cov.client_addr
            request.subscriberProcessIdentifier = cov.proc_id
            request.initiatingDeviceIdentifier = self._app.localDevice.objectIdentifier
            request.monitoredObjectIdentifier = cov.obj_id
            request.timeRemaining = time_remaining
            request.listOfValues = list_of_values
            if _debug: COVObjectMixin._debug("    - request: %r", request)

            # let the application send it
            self._app.cov_notification(cov, request)

# ---------------------------
# access door
# ---------------------------

@bacpypes_debugging
class AccessDoorCriteria(COVCriteria):

    _properties_tracked = (
        'presentValue',
        'statusFlags',
        'doorAlarmState',
        )
    _properties_reported = (
        'presentValue',
        'statusFlags',
        'doorAlarmState',
        )

@register_object_type
class AccessDoorObjectCOV(COVObjectMixin, AccessDoorCriteria, AccessDoorObject):
    pass

# ---------------------------
# access point
# ---------------------------

@bacpypes_debugging
class AccessPointCriteria(COVCriteria):

    _properties_tracked = (
        'accessEventTime',
        'statusFlags',
        )
    _properties_reported = (
        'accessEvent',
        'statusFlags',
        'accessEventTag',
        'accessEventTime',
        'accessEventCredential',
        'accessEventAuthenticationFactor',
        )
    _monitored_property_reference = 'accessEvent'

@register_object_type
class AccessPointObjectCOV(COVObjectMixin, AccessPointCriteria, AccessPointObject):
    pass

# ---------------------------
# analog objects
# ---------------------------

@register_object_type
class AnalogInputObjectCOV(COVObjectMixin, COVIncrementCriteria, AnalogInputObject):
    pass

@register_object_type
class AnalogOutputObjectCOV(COVObjectMixin, COVIncrementCriteria, AnalogOutputObject):
    pass

@register_object_type
class AnalogValueObjectCOV(COVObjectMixin, COVIncrementCriteria, AnalogValueObject):
    pass

@register_object_type
class LargeAnalogValueObjectCOV(COVObjectMixin, COVIncrementCriteria, LargeAnalogValueObject):
    pass

@register_object_type
class IntegerValueObjectCOV(COVObjectMixin, COVIncrementCriteria, IntegerValueObject):
    pass

@register_object_type
class PositiveIntegerValueObjectCOV(COVObjectMixin, COVIncrementCriteria, PositiveIntegerValueObject):
    pass

@register_object_type
class LightingOutputObjectCOV(COVObjectMixin, COVIncrementCriteria, LightingOutputObject):
    pass

# ---------------------------
# generic objects
# ---------------------------

@register_object_type
class BinaryInputObjectCOV(COVObjectMixin, GenericCriteria, BinaryInputObject):
    pass

@register_object_type
class BinaryOutputObjectCOV(COVObjectMixin, GenericCriteria, BinaryOutputObject):
    pass

@register_object_type
class BinaryValueObjectCOV(COVObjectMixin, GenericCriteria, BinaryValueObject):
    pass

@register_object_type
class LifeSafetyPointObjectCOV(COVObjectMixin, GenericCriteria, LifeSafetyPointObject):
    pass

@register_object_type
class LifeSafetyZoneObjectCOV(COVObjectMixin, GenericCriteria, LifeSafetyZoneObject):
    pass

@register_object_type
class MultiStateInputObjectCOV(COVObjectMixin, GenericCriteria, MultiStateInputObject):
    pass

@register_object_type
class MultiStateOutputObjectCOV(COVObjectMixin, GenericCriteria, MultiStateOutputObject):
    pass

@register_object_type
class MultiStateValueObjectCOV(COVObjectMixin, GenericCriteria, MultiStateValueObject):
    pass

@register_object_type
class OctetStringValueObjectCOV(COVObjectMixin, GenericCriteria, OctetStringValueObject):
    pass

@register_object_type
class CharacterStringValueObjectCOV(COVObjectMixin, GenericCriteria, CharacterStringValueObject):
    pass

@register_object_type
class TimeValueObjectCOV(COVObjectMixin, GenericCriteria, TimeValueObject):
    pass

@register_object_type
class DateTimeValueObjectCOV(COVObjectMixin, GenericCriteria, DateTimeValueObject):
    pass

@register_object_type
class DateValueObjectCOV(COVObjectMixin, GenericCriteria, DateValueObject):
    pass

@register_object_type
class TimePatternValueObjectCOV(COVObjectMixin, GenericCriteria, TimePatternValueObject):
    pass

@register_object_type
class DatePatternValueObjectCOV(COVObjectMixin, GenericCriteria, DatePatternValueObject):
    pass

@register_object_type
class DateTimePatternValueObjectCOV(COVObjectMixin, GenericCriteria, DateTimePatternValueObject):
    pass

# ---------------------------
# credential data input
# ---------------------------

@bacpypes_debugging
class CredentialDataInputCriteria(COVCriteria):

    _properties_tracked = (
        'updateTime',
        'statusFlags'
        )
    _properties_reported = (
        'presentValue',
        'statusFlags',
        'updateTime',
        )

@register_object_type
class CredentialDataInputObjectCOV(COVObjectMixin, CredentialDataInputCriteria, CredentialDataInputObject):
    pass

# ---------------------------
# load control
# ---------------------------

@bacpypes_debugging
class LoadControlCriteria(COVCriteria):

    _properties_tracked = (
        'presentValue',
        'statusFlags',
        'requestedShedLevel',
        'startTime',
        'shedDuration',
        'dutyWindow',
        )
    _properties_reported = (
        'presentValue',
        'statusFlags',
        'requestedShedLevel',
        'startTime',
        'shedDuration',
        'dutyWindow',
        )

@register_object_type
class LoadControlObjectCOV(COVObjectMixin, LoadControlCriteria, LoadControlObject):
    pass

# ---------------------------
# loop
# ---------------------------

@register_object_type
class LoopObjectCOV(COVObjectMixin, COVIncrementCriteria, LoopObject):
    pass

# ---------------------------
# pulse converter
# ---------------------------

@bacpypes_debugging
class PulseConverterCriteria():

    _properties_tracked = (
        'presentValue',
        'statusFlags',
        )
    _properties_reported = (
        'presentValue',
        'statusFlags',
        )

@register_object_type
class PulseConverterObjectCOV(COVObjectMixin, PulseConverterCriteria, PulseConverterObject):
    pass

#
#   COVApplicationMixin
#

@bacpypes_debugging
class COVApplicationMixin(object):

    def __init__(self, *args, **kwargs):
        if _debug: COVApplicationMixin._debug("__init__ %r %r", args, kwargs)
        super(COVApplicationMixin, self).__init__(*args, **kwargs)

        # list of active subscriptions
        self.active_cov_subscriptions = []

        # a queue of confirmed notifications by client address
        self.confirmed_notifications_queue = defaultdict(list)

    def cov_notification(self, cov, request):
        if _debug: COVApplicationMixin._debug("cov_notification %s %s", str(cov), str(request))

        # if this is confirmed, keep track of the cov
        if cov.confirmed:
            if _debug: COVApplicationMixin._debug("    - it's confirmed")

            notification_list = self.confirmed_notifications_queue[cov.client_addr]
            notification_list.append((request, cov))

            # if this isn't the first, wait until the first one is done
            if len(notification_list) > 1:
                if _debug: COVApplicationMixin._debug("    - not the first")
                return
        else:
            if _debug: COVApplicationMixin._debug("    - it's unconfirmed")

        # send it along down the stack
        super(COVApplicationMixin, self).request(request)
        if _debug: COVApplicationMixin._debug("    - apduInvokeID: %r", getattr(request, 'apduInvokeID'))

    def cov_error(self, cov, request, response):
        if _debug: COVApplicationMixin._debug("cov_error %r %r %r", cov, request, response)

    def cov_reject(self, cov, request, response):
        if _debug: COVApplicationMixin._debug("cov_reject %r %r %r", cov, request, response)

    def cov_abort(self, cov, request, response):
        if _debug: COVApplicationMixin._debug("cov_abort %r %r %r", cov, request, response)

        # delete the rest of the pending requests for this client
        del self.confirmed_notifications_queue[cov.client_addr][:]
        if _debug: COVApplicationMixin._debug("    - other notifications deleted")

    def confirmation(self, apdu):
        if _debug: COVApplicationMixin._debug("confirmation %r", apdu)

        if _debug: COVApplicationMixin._debug("    - queue keys: %r", self.confirmed_notifications_queue.keys())

        # if this isn't from someone we care about, toss it
        if apdu.pduSource not in self.confirmed_notifications_queue:
            if _debug: COVApplicationMixin._debug("    - not someone we are tracking")

            # pass along to the application
            super(COVApplicationMixin, self).confirmation(apdu)
            return

        # refer to the notification list for this client
        notification_list = self.confirmed_notifications_queue[apdu.pduSource]
        if _debug: COVApplicationMixin._debug("    - notification_list: %r", notification_list)

        # peek at the front of the list
        request, cov = notification_list[0]
        if _debug: COVApplicationMixin._debug("    - request: %s", request)

        # line up the invoke id
        if apdu.apduInvokeID == request.apduInvokeID:
            if _debug: COVApplicationMixin._debug("    - request/response align")
            notification_list.pop(0)
        else:
            if _debug: COVApplicationMixin._debug("    - request/response do not align")

            # pass along to the application
            super(COVApplicationMixin, self).confirmation(apdu)
            return

        if isinstance(apdu, Error):
            if _debug: COVApplicationMixin._debug("    - error: %r", apdu.errorCode)
            self.cov_error(cov, request, apdu)

        elif isinstance(apdu, RejectPDU):
            if _debug: COVApplicationMixin._debug("    - reject: %r", apdu.apduAbortRejectReason)
            self.cov_reject(cov, request, apdu)

        elif isinstance(apdu, AbortPDU):
            if _debug: COVApplicationMixin._debug("    - abort: %r", apdu.apduAbortRejectReason)
            self.cov_abort(cov, request, apdu)

        # if the notification list is empty, delete the reference
        if not notification_list:
            if _debug: COVApplicationMixin._debug("    - no other pending notifications")
            del self.confirmed_notifications_queue[apdu.pduSource]
            return

        # peek at the front of the list for the next request
        request, cov = notification_list[0]
        if _debug: COVApplicationMixin._debug("    - next notification: %r", request)

        # send it along down the stack
        super(COVApplicationMixin, self).request(request)

    def do_SubscribeCOVRequest(self, apdu):
        if _debug: COVApplicationMixin._debug("do_SubscribeCOVRequest %r", apdu)

        # extract the pieces
        client_addr = apdu.pduSource
        proc_id = apdu.subscriberProcessIdentifier
        obj_id = apdu.monitoredObjectIdentifier
        confirmed = apdu.issueConfirmedNotifications
        lifetime = apdu.lifetime

        # request is to cancel the subscription
        cancel_subscription = (confirmed is None) and (lifetime is None)

        # find the object
        obj = self.get_object_id(obj_id)
        if not obj:
            if _debug: COVConsoleCmd._debug("    - object not found")
            self.response(Error(errorClass='object', errorCode='unknownObject', context=apdu))
            return

        # can a match be found?
        cov = obj._cov_subscriptions.find(client_addr, proc_id, obj_id)
        if _debug: COVConsoleCmd._debug("    - cov: %r", cov)

        # if a match was found, update the subscription
        if cov:
            if cancel_subscription:
                if _debug: COVConsoleCmd._debug("    - cancel the subscription")
                cov.cancel_subscription()
            else:
                if _debug: COVConsoleCmd._debug("    - renew the subscription")
                cov.renew_subscription(lifetime)
        else:
            if cancel_subscription:
                if _debug: COVConsoleCmd._debug("    - cancel a subscription that doesn't exist")
            else:
                if _debug: COVConsoleCmd._debug("    - create a subscription")

                cov = Subscription(obj, client_addr, proc_id, obj_id, confirmed, lifetime)
                if _debug: COVConsoleCmd._debug("    - cov: %r", cov)

        # success
        response = SimpleAckPDU(context=apdu)

        # return the result
        self.response(response)

#
#   ActiveCOVSubscriptions
#

@bacpypes_debugging
class ActiveCOVSubscriptions(Property):

    def __init__(self, identifier):
        Property.__init__(
            self, identifier, SequenceOf(COVSubscription),
            default=None, optional=True, mutable=False,
            )

    def ReadProperty(self, obj, arrayIndex=None):
        if _debug: ActiveCOVSubscriptions._debug("ReadProperty %s arrayIndex=%r", obj, arrayIndex)

        # get the current time from the task manager
        current_time = TaskManager().get_time()
        if _debug: ActiveCOVSubscriptions._debug("    - current_time: %r", current_time)

        # start with an empty sequence
        cov_subscriptions = SequenceOf(COVSubscription)()

        # the obj is a DeviceObject with a reference to the application
        for cov in obj._app.active_cov_subscriptions:
            # calculate time remaining
            if not cov.lifetime:
                time_remaining = 0
            else:
                time_remaining = int(cov.taskTime - current_time)

                # make sure it is at least one second
                if not time_remaining:
                    time_remaining = 1

            recipient_process = RecipientProcess(
                recipient=Recipient(
                    address=DeviceAddress(
                        networkNumber=cov.client_addr.addrNet or 0,
                        macAddress=cov.client_addr.addrAddr,
                        ),
                    ),
                processIdentifier=cov.proc_id,
                )

            cov_subscription = COVSubscription(
                recipient=recipient_process,
                monitoredPropertyReference=ObjectPropertyReference(
                    objectIdentifier=cov.obj_id,
                    propertyIdentifier=cov.obj_ref._monitored_property_reference,
                    ),
                issueConfirmedNotifications=cov.confirmed,
                timeRemaining=time_remaining,
                # covIncrement=???,
                )
            if _debug: ActiveCOVSubscriptions._debug("    - cov_subscription: %r", cov_subscription)

            # add the list
            cov_subscriptions.append(cov_subscription)

        return cov_subscriptions

    def WriteProperty(self, obj, value, arrayIndex=None, priority=None):
        raise ExecutionError(errorClass='property', errorCode='writeAccessDenied')

#
#   COVDeviceObject
#

@bacpypes_debugging
class COVDeviceMixin(object):

    properties = [
        ActiveCOVSubscriptions('activeCovSubscriptions'),
        ]

class LocalDeviceObjectCOV(COVDeviceMixin, LocalDeviceObject):
    pass

#
#   SubscribeCOVApplication
#

@bacpypes_debugging
class SubscribeCOVApplication(COVApplicationMixin, BIPSimpleApplication):
    pass

#
#   COVConsoleCmd
#

@bacpypes_debugging
class COVConsoleCmd(ConsoleCmd):

    def do_subscribe(self, args):
        """subscribe addr proc_id obj_type obj_inst [ confirmed ] [ lifetime ]
        """
        args = args.split()
        if _debug: COVConsoleCmd._debug("do_subscribe %r", args)
        global test_application

        try:
            addr, proc_id, obj_type, obj_inst = args[:4]

            client_addr = Address(addr)
            if _debug: COVConsoleCmd._debug("    - client_addr: %r", client_addr)

            proc_id = int(proc_id)
            if _debug: COVConsoleCmd._debug("    - proc_id: %r", proc_id)

            if obj_type.isdigit():
                obj_type = int(obj_type)
            elif not get_object_class(obj_type):
                raise ValueError("unknown object type")
            obj_inst = int(obj_inst)
            obj_id = (obj_type, obj_inst)
            if _debug: COVConsoleCmd._debug("    - obj_id: %r", obj_id)

            obj = test_application.get_object_id(obj_id)
            if not obj:
                print("object not found")
                return

            if len(args) >= 5:
                issue_confirmed = args[4]
                if issue_confirmed == '-':
                    issue_confirmed = None
                else:
                    issue_confirmed = issue_confirmed.lower() == 'true'
                if _debug: COVConsoleCmd._debug("    - issue_confirmed: %r", issue_confirmed)
            else:
                issue_confirmed = None

            if len(args) >= 6:
                lifetime = args[5]
                if lifetime == '-':
                    lifetime = None
                else:
                    lifetime = int(lifetime)
                if _debug: COVConsoleCmd._debug("    - lifetime: %r", lifetime)
            else:
                lifetime = None

            # can a match be found?
            cov = obj._cov_subscriptions.find(client_addr, proc_id, obj_id)
            if _debug: COVConsoleCmd._debug("    - cov: %r", cov)

            # build a request
            request = SubscribeCOVRequest(
                subscriberProcessIdentifier=proc_id,
                monitoredObjectIdentifier=obj_id,
                )

            # spoof that it came from the client
            request.pduSource = client_addr

            # optional parameters
            if issue_confirmed is not None:
                request.issueConfirmedNotifications = issue_confirmed
            if lifetime is not None:
                request.lifetime = lifetime

            if _debug: COVConsoleCmd._debug("    - request: %r", request)

            # give it to the application
            test_application.do_SubscribeCOVRequest(request)

        except Exception as err:
            COVConsoleCmd._exception("exception: %r", err)

    def do_status(self, args):
        """status [ object_name ]"""
        args = args.split()
        if _debug: COVConsoleCmd._debug("do_status %r", args)
        global test_application

        if args:
            obj = test_application.get_object_name(args[0])
            if not obj:
                print("no such object")
            else:
                print("%s %s" % (obj.objectName, obj.objectIdentifier))
                obj.debug_contents()
        else:
            # dump the information about all the known objects
            for obj in test_application.iter_objects():
                print("%s %s" % (obj.objectName, obj.objectIdentifier))
                obj.debug_contents()

    def do_trigger(self, args):
        """trigger object_name"""
        args = args.split()
        if _debug: COVConsoleCmd._debug("do_trigger %r", args)
        global test_application

        if not args:
            print("object name required")
        else:
            obj = test_application.get_object_name(args[0])
            if not obj:
                print("no such object")
            else:
                obj._send_cov_notifications()

    def do_set(self, args):
        """set object_name [ . ] property_name [ = ] value"""
        args = args.split()
        if _debug: COVConsoleCmd._debug("do_set %r", args)
        global test_application

        try:
            object_name = args.pop(0)
            if '.' in object_name:
                object_name, property_name = object_name.split('.')
            else:
                property_name = args.pop(0)
            if _debug: COVConsoleCmd._debug("    - object_name: %r", object_name)
            if _debug: COVConsoleCmd._debug("    - property_name: %r", property_name)

            obj = test_application.get_object_name(object_name)
            if _debug: COVConsoleCmd._debug("    - obj: %r", obj)
            if not obj:
                raise RuntimeError("object not found: %r" % (object_name,))

            datatype = obj.get_datatype(property_name)
            if _debug: COVConsoleCmd._debug("    - datatype: %r", datatype)
            if not datatype:
                raise RuntimeError("not a property: %r" % (property_name,))

            # toss the equals
            if args[0] == '=':
                args.pop(0)

            # evaluate the value
            value = eval(args.pop(0))
            if _debug: COVConsoleCmd._debug("    - raw value: %r", value)

            # see if it can be built
            obj_value = datatype(value)
            if _debug: COVConsoleCmd._debug("    - obj_value: %r", obj_value)

            # normalize
            value = obj_value.value
            if _debug: COVConsoleCmd._debug("    - normalized value: %r", value)

            # change the value
            setattr(obj, property_name, value)

        except IndexError:
            print(COVConsoleCmd.do_set.__doc__)
        except Exception as err:
            print("exception: %s" % (err,))

    def do_write(self, args):
        """write object_name [ . ] property [ = ] value"""
        args = args.split()
        if _debug: COVConsoleCmd._debug("do_set %r", args)
        global test_application

        try:
            object_name = args.pop(0)
            if '.' in object_name:
                object_name, property_name = object_name.split('.')
            else:
                property_name = args.pop(0)
            if _debug: COVConsoleCmd._debug("    - object_name: %r", object_name)
            if _debug: COVConsoleCmd._debug("    - property_name: %r", property_name)

            obj = test_application.get_object_name(object_name)
            if _debug: COVConsoleCmd._debug("    - obj: %r", obj)
            if not obj:
                raise RuntimeError("object not found: %r" % (object_name,))

            datatype = obj.get_datatype(property_name)
            if _debug: COVConsoleCmd._debug("    - datatype: %r", datatype)
            if not datatype:
                raise RuntimeError("not a property: %r" % (property_name,))

            # toss the equals
            if args[0] == '=':
                args.pop(0)

            # evaluate the value
            value = eval(args.pop(0))
            if _debug: COVConsoleCmd._debug("    - raw value: %r", value)

            # see if it can be built
            obj_value = datatype(value)
            if _debug: COVConsoleCmd._debug("    - obj_value: %r", obj_value)

            # normalize
            value = obj_value.value
            if _debug: COVConsoleCmd._debug("    - normalized value: %r", value)

            # pass it along
            obj.WriteProperty(property_name, value)

        except IndexError:
            print(COVConsoleCmd.do_write.__doc__)
        except Exception as err:
            print("exception: %s" % (err,))


def main():
    global test_application

    # make a parser
    parser = ConfigArgumentParser(description=__doc__)
    parser.add_argument("--console",
        action="store_true",
        default=False,
        help="create a console",
        )

    # parse the command line arguments
    args = parser.parse_args()

    if _debug: _log.debug("initialization")
    if _debug: _log.debug("    - args: %r", args)

    # make a device object
    test_device = LocalDeviceObjectCOV(
        objectName=args.ini.objectname,
        objectIdentifier=int(args.ini.objectidentifier),
        maxApduLengthAccepted=int(args.ini.maxapdulengthaccepted),
        segmentationSupported=args.ini.segmentationsupported,
        vendorIdentifier=int(args.ini.vendoridentifier),
        )

    # make a sample application
    test_application = SubscribeCOVApplication(test_device, args.ini.address)

    # make a binary value object
    test_bvo = BinaryValueObjectCOV(
        objectIdentifier=('binaryValue', 1),
        objectName='bvo',
        presentValue='inactive',
        statusFlags=[0, 0, 0, 0],
        )
    _log.debug("    - test_bvo: %r", test_bvo)

    # add it to the device
    test_application.add_object(test_bvo)

    # make an analog value object
    test_avo = AnalogValueObjectCOV(
        objectIdentifier=('analogValue', 1),
        objectName='avo',
        presentValue=0.0,
        statusFlags=[0, 0, 0, 0],
        covIncrement=1.0,
        )
    _log.debug("    - test_avo: %r", test_avo)

    # add it to the device
    test_application.add_object(test_avo)
    _log.debug("    - object list: %r", test_device.objectList)

    # get the services supported
    services_supported = test_application.get_services_supported()
    if _debug: _log.debug("    - services_supported: %r", services_supported)

    # let the device object know
    test_device.protocolServicesSupported = services_supported.value

    # make a console
    if args.console:
        test_console = COVConsoleCmd()
        _log.debug("    - test_console: %r", test_console)

        # enable sleeping will help with threads
        enable_sleeping()

    _log.debug("running")

    run()

    _log.debug("fini")


if __name__ == "__main__":
    main()
