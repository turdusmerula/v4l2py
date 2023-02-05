#
# This file is part of the v4l2py project
#
# Copyright (c) 2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

import collections
import enum
import errno
import fcntl
import fractions
import logging
import mmap
import os
import pathlib
import select

from . import raw

log = logging.getLogger(__name__)
log_ioctl = log.getChild("ioctl")
log_mmap = log.getChild("mmap")

class V4L2Error(Exception):
    pass


def _enum(name, prefix, klass=enum.IntEnum):
    return klass(
        name,
        (
            (name.replace(prefix, ""), getattr(raw, name))
            for name in dir(raw)
            if name.startswith(prefix)
        ),
    )


Capability = _enum("Capability", "V4L2_CAP_", klass=enum.IntFlag)
PixelFormat = _enum("PixelFormat", "V4L2_PIX_FMT_")
BufferType = _enum("BufferType", "V4L2_BUF_TYPE_")
Memory = _enum("Memory", "V4L2_MEMORY_")
ImageFormatFlag = _enum("ImageFormatFlag", "V4L2_FMT_FLAG_", klass=enum.IntFlag)
Field = _enum("Field", "V4L2_FIELD_")
FrameSizeType = _enum("FrameSizeType", "V4L2_FRMSIZE_TYPE_")
FrameIntervalType = _enum("FrameIntervalType", "V4L2_FRMIVAL_TYPE_")
IOC = _enum("IOC", "VIDIOC_", klass=enum.Enum)


def human_pixel_format(ifmt):
    return "".join(map(chr, ((ifmt >> i) & 0xFF for i in range(0, 4 * 8, 8))))


PixelFormat.human_str = lambda self: human_pixel_format(self.value)


Info = collections.namedtuple(
    "Info",
    "driver card bus_info version capabilities device_capabilities crop_capabilities buffers formats frame_sizes",
)

ImageFormat = collections.namedtuple(
    "ImageFormat", "type description flags pixel_format"
)

Format = collections.namedtuple("Format", "width height pixel_format")

CropCapability = collections.namedtuple(
    "CropCapability", "type bounds defrect pixel_aspect"
)

Rect = collections.namedtuple("Rect", "left top width height")

Size = collections.namedtuple("Size", "width height")

FrameType = collections.namedtuple(
    "FrameType", "type pixel_format width height min_fps max_fps step_fps"
)


INFO_REPR = """\
driver = {info.driver}
card = {info.card}
bus = {info.bus_info}
version = {info.version}
capabilities = {capabilities}
device_capabilities = {device_capabilities}
buffers = {buffers}
"""

def ioctl(fd, request, arg):
    log_ioctl.debug("%d, request=%s, arg=%s", fd, request.name, arg)
    return fcntl.ioctl(fd, request.value, arg)


def mem_map(fd, length, offset):
    log_mmap.debug("%d, length=%d, offset=%d", fd, length, offset)
    return mmap.mmap(fd, length, offset=offset)


def flag_items(flag):
    return [item for item in type(flag) if item in flag]


def Info_repr(info):
    dcaps = "|".join(cap.name for cap in flag_items(info.device_capabilities))
    caps = "|".join(cap.name for cap in flag_items(info.capabilities))
    buffers = "|".join(buff.name for buff in info.buffers)
    return INFO_REPR.format(
        info=info, capabilities=caps, device_capabilities=dcaps, buffers=buffers
    )


Info.__repr__ = Info_repr


def raw_crop_caps_to_crop_caps(stream_type, crop):
    return CropCapability(
        type=stream_type,
        bounds=Rect(
            crop.bounds.left,
            crop.bounds.top,
            crop.bounds.width,
            crop.bounds.height,
        ),
        defrect=Rect(
            crop.defrect.left,
            crop.defrect.top,
            crop.defrect.width,
            crop.defrect.height,
        ),
        pixel_aspect=crop.pixelaspect.numerator / crop.pixelaspect.denominator,
    )


CropCapability.from_raw = raw_crop_caps_to_crop_caps


def iter_read(fd, ioc, indexed_struct, start=0, stop=128, step=1):
    for index in range(start, stop, step):
        indexed_struct.index = index
        try:
            ioctl(fd, ioc, indexed_struct)
            yield indexed_struct
        except OSError as error:
            if error.errno == errno.EINVAL:
                break
            else:
                raise


def frame_sizes(fd, pixel_formats):
    def get_frame_intervals(fmt, w, h):
        val = raw.v4l2_frmivalenum()
        val.pixel_format = fmt
        val.width = w
        val.height = h
        res = []
        for val in iter_read(fd, IOC.ENUM_FRAMEINTERVALS, val):
            # values come in frame interval (fps = 1/interval)
            try:
                ftype = FrameIntervalType(val.type)
            except ValueError:
                break
            if ftype == FrameIntervalType.DISCRETE:
                min_fps = max_fps = step_fps = fractions.Fraction(
                    val.discrete.denominator / val.discrete.numerator
                )
            else:
                if val.stepwise.min.numerator == 0:
                    min_fps = 0
                else:
                    min_fps = fractions.Fraction(
                        val.stepwise.min.denominator, val.stepwise.min.numerator
                    )
                if val.stepwise.max.numerator == 0:
                    max_fps = 0
                else:
                    max_fps = fractions.Fraction(
                        val.stepwise.max.denominator, val.stepwise.max.numerator
                    )
                if val.stepwise.step.numerator == 0:
                    step_fps = 0
                else:
                    step_fps = fractions.Fraction(
                        val.stepwise.step.denominator, val.stepwise.step.numerator
                    )
            res.append(
                FrameType(
                    type=ftype,
                    pixel_format=fmt,
                    width=w,
                    height=h,
                    min_fps=min_fps,
                    max_fps=max_fps,
                    step_fps=step_fps,
                )
            )
        return res

    size = raw.v4l2_frmsizeenum()
    sizes = []
    for pixel_format in pixel_formats:
        size.pixel_format = pixel_format
        size.index = 0
        while True:
            try:
                ioctl(fd, IOC.ENUM_FRAMESIZES, size)
            except OSError:
                break
            if size.type == FrameSizeType.DISCRETE:
                sizes += get_frame_intervals(
                    pixel_format, size.discrete.width, size.discrete.height
                )
            size.index += 1
    return sizes


def read_capabilities(fd):
    caps = raw.v4l2_capability()
    ioctl(fd, IOC.QUERYCAP, caps)
    return caps


def iter_read_formats(fd, type):
    fmt = raw.v4l2_fmtdesc()
    fmt.type = type
    pixel_formats = set(PixelFormat)
    for fmt in iter_read(fd, IOC.ENUM_FMT, fmt):
        pixel_fmt = fmt.pixelformat
        if pixel_fmt not in pixel_formats:
            log.debug(
                "unknown pixel format %s (%d)", human_pixel_format(pixel_fmt), pixel_fmt
            )
            continue
        image_format = ImageFormat(
            type=type,
            flags=ImageFormatFlag(fmt.flags),
            description=fmt.description.decode(),
            pixel_format=PixelFormat(pixel_fmt),
        )
        yield image_format


def read_info(fd):
    caps = read_capabilities(fd)
    version_tuple = (
        (caps.version & 0xFF0000) >> 16,
        (caps.version & 0x00FF00) >> 8,
        (caps.version & 0x0000FF),
    )
    version_str = ".".join(map(str, version_tuple))
    device_capabilities = Capability(caps.device_caps)
    buffers = [typ for typ in BufferType if Capability[typ.name] in device_capabilities]

    fmt = raw.v4l2_fmtdesc()
    img_fmt_stream_types = {
        BufferType.VIDEO_CAPTURE,
        BufferType.VIDEO_CAPTURE_MPLANE,
        BufferType.VIDEO_OUTPUT,
        BufferType.VIDEO_OUTPUT_MPLANE,
        BufferType.VIDEO_OVERLAY,
    } & set(buffers)

    image_formats = []
    pixel_formats = set()
    for stream_type in img_fmt_stream_types:
        for image_format in iter_read_formats(fd, stream_type):
            image_formats.append(image_format)
            pixel_formats.add(image_format.pixel_format)

    crop = raw.v4l2_cropcap()
    crop_stream_types = {
        BufferType.VIDEO_CAPTURE,
        BufferType.VIDEO_OUTPUT,
        BufferType.VIDEO_OVERLAY,
    } & set(buffers)
    crop_caps = []
    for stream_type in crop_stream_types:
        crop.type = stream_type
        try:
            ioctl(fd, IOC.CROPCAP, crop)
        except OSError:
            continue
        crop_cap = CropCapability.from_raw(stream_type, crop)
        crop_caps.append(crop_cap)

    return Info(
        driver=caps.driver.decode(),
        card=caps.card.decode(),
        bus_info=caps.bus_info.decode(),
        version=version_str,
        capabilities=Capability(caps.capabilities),
        device_capabilities=device_capabilities,
        crop_capabilities=crop_caps,
        buffers=buffers,
        formats=image_formats,
        frame_sizes=frame_sizes(fd, pixel_formats),
    )


def query_buffer(fd, buffer_type: BufferType, memory: Memory, index: int) -> raw.v4l2_buffer:
    buff = raw.v4l2_buffer()
    buff.type = buffer_type
    buff.memory = memory
    buff.index = index
    buff.reserved = 0
    ioctl(fd, IOC.QUERYBUF, buff)
    return buff
    

def enqueue_buffer(fd, buffer_type: BufferType, memory: Memory, index: int) -> raw.v4l2_buffer:
    buff = raw.v4l2_buffer()
    buff.type = buffer_type
    buff.memory = memory
    buff.index = index
    buff.reserved = 0
    ioctl(fd, IOC.QBUF, buff)
    return buff


def dequeue_buffer(fd, buffer_type: BufferType, memory: Memory) -> raw.v4l2_buffer:
    buff = raw.v4l2_buffer()
    buff.type = buffer_type
    buff.memory = memory
    buff.index = 0
    buff.reserved = 0
    ioctl(fd, IOC.DQBUF, buff)
    return buff


def request_buffers(fd, buffer_type: BufferType, memory: Memory, count: int) -> raw.v4l2_requestbuffers:
    req = raw.v4l2_requestbuffers()
    req.type = buffer_type
    req.memory = memory
    req.count = count
    ioctl(fd, IOC.REQBUFS, req)
    if not req.count:
        raise IOError("Not enough buffer memory")
    return req


def free_buffers(fd, buffer_type: BufferType, memory: Memory) -> raw.v4l2_requestbuffers:
    req = raw.v4l2_requestbuffers()
    req.type = buffer_type
    req.memory = memory
    req.count = 0
    ioctl(fd, IOC.REQBUFS, req)
    return req


def set_format(fd, buffer_type, width, height, pixel_format="MJPG"):
    f = raw.v4l2_format()
    if isinstance(pixel_format, str):
        pixel_format = raw.v4l2_fourcc(*pixel_format.upper())
    f.type = buffer_type
    f.fmt.pix.pixelformat = pixel_format
    f.fmt.pix.field = Field.ANY
    f.fmt.pix.width = width
    f.fmt.pix.height = height
    f.fmt.pix.bytesperline = 0
    return ioctl(fd, IOC.S_FMT, f)


def get_format(fd, buffer_type):
    f = raw.v4l2_format()
    f.type = buffer_type
    ioctl(fd, IOC.G_FMT, f)
    return Format(
        width=f.fmt.pix.width,
        height=f.fmt.pix.height,
        pixel_format=PixelFormat(f.fmt.pix.pixelformat)
    )


def set_fps(fd, buffer_type, fps):
    p = raw.v4l2_streamparm()
    p.type = buffer_type
    fps = fractions.Fraction(fps)
    p.parm.capture.timeperframe.numerator = fps.denominator
    p.parm.capture.timeperframe.denominator = fps.numerator
    return ioctl(fd, IOC.S_PARM, p)


def get_fps(fd, buffer_type):
    p = raw.v4l2_streamparm()
    p.type = buffer_type
    ioctl(fd, IOC.G_PARM, p)
    return fractions.Fraction(p.parm.capture.timeperframe.denominator, p.parm.capture.timeperframe.numerator)


def stream_on(fd, buffer_type):
    btype = raw.v4l2_buf_type(buffer_type)
    return ioctl(fd, IOC.STREAMON, btype)


def stream_off(fd, buffer_type):
    btype = raw.v4l2_buf_type(buffer_type)
    return ioctl(fd, IOC.STREAMOFF, btype)


def fopen(path, rw=False):
    return open(path, "rb+" if rw else "rb", buffering=0, opener=opener)


def opener(path, flags):
    return os.open(path, flags | os.O_NONBLOCK)


# Helpers


def create_buffer(fd, buffer_type: BufferType, memory: Memory) -> raw.v4l2_buffer:
    """request + query buffers"""
    return create_buffer(fd, buffer_type, memory, 1)


def create_buffers(fd, buffer_type: BufferType, memory: Memory, count: int) -> list[raw.v4l2_buffer]:
    """request + query buffers"""
    request_buffers(fd, buffer_type, memory, count)
    return [query_buffer(fd, buffer_type, memory, index) for index in range(count)]


def mmap_from_buffer(fd, buff: raw.v4l2_buffer) -> mmap.mmap:
    return mem_map(fd, buff.length, offset=buff.m.offset)


def create_mmap_buffers(fd, buffer_type: BufferType, memory: Memory, count: int) -> list[mmap.mmap]:
    """create buffers + mmap_from_buffer"""
    return [mmap_from_buffer(fd, buff) for buff in create_buffers(fd, buffer_type, memory, count)]


def create_mmap_buffer(fd, buffer_type: BufferType, memory: Memory) -> mmap.mmap:
    return create_mmap_buffers(fd, buffer_type, memory, 1)


def enqueue_buffers(fd, buffer_type: BufferType, memory: Memory, count: int) -> list[raw.v4l2_buffer]:
    return [enqueue_buffer(fd, buffer_type, memory, index) for index in range(count)]


class ReentrantContextManager:

    def __init__(self):
        self._context_level = 0

    def __enter__(self):
        if not self._context_level:
            self._on_enter()
        self._context_level += 1
        return  self

    def __exit__(self, *exc):
        self._context_level -= 1
        if not self._context_level:
            self._on_exit(*exc)

    def _on_enter(self):
        self.open()

    def _on_exit(self, *exc):
        self.close()


class Device(ReentrantContextManager):
    def __init__(self, filename, read_write=True):
        super().__init__()
        filename = pathlib.Path(filename)
        self._log = log.getChild(filename.stem)
        self._read_write = read_write
        self._fobj = None
        self.filename = filename
        self.info = None

    def __repr__(self):
        return f"<{type(self).__name__} name={self.filename}, closed={self.closed}>"

    def _ioctl(self, request, arg=0):
        return ioctl(self.fileno(), request, arg)

    @classmethod
    def from_id(cls, did):
        return cls("/dev/video{}".format(did))

    def open(self):
        if not self._fobj:
            self._log.info("opening %s", self.filename)
            self._fobj = fopen(self.filename, self._read_write)
            self.info = read_info(self.fileno())
            self._log.info("opened %s (%s)", self.filename, self.info.card)

    def close(self):
        if not self.closed:
            self._log.info("closing %s (%s)", self.filename, self.info.card)
            self._fobj.close()
            self._fobj = None
            self.info = None

    def fileno(self):
        return self._fobj.fileno()

    @property
    def closed(self):
        return self._fobj is None or self._fobj.closed

    def query_buffer(self, buffer_type, memory, index):
        return query_buffer(self.fileno(), buffer_type, memory, index)

    def enqueue_buffer(self, buffer_type: BufferType, memory: Memory, index: int) -> raw.v4l2_buffer:
        return enqueue_buffer(self.fileno(), buffer_type, memory, index)

    def dequeue_buffer(self, buffer_type: BufferType, memory: Memory) -> raw.v4l2_buffer:
        return dequeue_buffer(self.fileno(), buffer_type, memory)

    def request_buffers(self, buffer_type, memory, size):
        return request_buffers(self.fileno(), buffer_type, memory, size)

    def free_buffers(self, buffer_type, memory):
        return free_buffers(self.fileno(), buffer_type, memory)

    def set_format(self, buffer_type, width, height, pixel_format="MJPG"):
        return set_format(self.fileno(), buffer_type, width, height, pixel_format="MJPG")

    def get_format(self, buffer_type):
        return get_format(self.fileno(), buffer_type)

    def set_fps(self, buffer_type, fps):
        return set_fps(self.fileno(), buffer_type, fps)

    def get_fps(self, buffer_type):
        return get_fps(self.fileno(), buffer_type)

    def stream_on(self, buffer_type):
        stream_on(self.fileno(), buffer_type)

    def stream_off(self, buffer_type):
        stream_off(self.fileno(), buffer_type)


class DeviceHelper:
    def __init__(self, device: Device):
        super().__init__()
        self.device = device


class BufferManager(DeviceHelper):
    def __init__(self, device: Device, buffer_type):
        super().__init__(device)
        self.buffer_type = buffer_type

    def formats(self):
        formats = self.device.info.formats
        return [fmt for fmt in formats if fmt.type == self.buffer_type]

    def crop_capabilities(self):
        crop_capabilities = self.device.info.crop_capabilities
        return [crop for crop in crop_capabilities if crop.type == self.buffer_type]

    def query_buffer(self, memory, index):
        return self.device.query_buffer(self.buffer_type, memory, index)

    def enqueue_buffer(self, memory: Memory, index: int) -> raw.v4l2_buffer:
        return self.device.enqueue_buffer(self.buffer_type, memory, index)

    def dequeue_buffer(self, memory: Memory) -> raw.v4l2_buffer:
        return self.device.dequeue_buffer(self.buffer_type, memory)

    def request_buffers(self, memory, size):
        return self.device.request_buffers(self.buffer_type, memory, size)

    def free_buffers(self, memory):
        return self.device.free_buffers(self.buffer_type, memory)

    def set_format(self, width, height, pixel_format="MJPG"):
        return self.device.set_format(self.buffer_type, width, height, pixel_format)

    def get_format(self):
        return self.device.get_format(self.buffer_type)

    def set_fps(self, fps):
        return self.device.set_fps(self.buffer_type, fps)

    def get_fps(self):
        return self.device.get_fps(self.buffer_type)

    def stream_on(self):
        self.device.stream_on(self.buffer_type)

    def stream_off(self):
        self.device.stream_off(self.buffer_type)

    start = stream_on
    stop = stream_off


class VideoCapture(BufferManager, ReentrantContextManager):
    def __init__(self, device: Device):
        super().__init__(device, BufferType.VIDEO_CAPTURE)
        self.buffer = None

    def __enter__(self):
        self.open()
        return  self.buffer

    def __exit__(self, *exc):
        self.close()

    def open(self):
        if self.buffer is None:
            self.buffer = MemoryMap(self, 10)
            self.buffer.open()
            self.stream_on()

    def close(self):
        if self.buffer:
            self.buffer.close()
            self.stream_off()

    def __iter__(self):
        yield from self.buffer


class QueueReader:
    def __init__(self, buffer_manager: BufferManager, memory: Memory):
        self.buffer_manager = buffer_manager
        self.memory = memory
        self.index = None

    def __enter__(self):
        # get next buffer that has some data in it
        buffer = self.buffer_manager.dequeue_buffer(self.memory)
        self.index = buffer.index
        return buffer

    def __exit__(self, *exc):
        self.buffer_manager.enqueue_buffer(self.memory, self.index)
        self.index = None


class MemoryMap(ReentrantContextManager):

    def __init__(self, buffer_manager: BufferManager, count=1):
        super().__init__()
        self.buffer_manager = buffer_manager
        self.count = count
        self.buffers = None
        self.reader = QueueReader(buffer_manager, Memory.MMAP)

    def __iter__(self):
        while True:
            yield self.read()

    def open(self):
        if self.buffers is None:
            fd = self.buffer_manager.device.fileno()
            buffer_type = self.buffer_manager.buffer_type
            self.buffers = create_mmap_buffers(fd, buffer_type, Memory.MMAP, self.count)
            enqueue_buffers(fd, buffer_type, Memory.MMAP, self.count)
        
    def close(self):
        if self.buffers:
            for mem in self.buffers:
                mem.close()
            self.buffer_manager.free_buffers(Memory.MMAP)
            self.buffers = None
    
    def raw_read(self):
        with self.reader as buff:
            return self.buffers[buff.index][:buff.bytesused]

    def read(self):
        select.select((self.buffer_manager.device,), (), ())
        return self.raw_read()


class BaseBuffer:
    def __init__(
        self, device, index=0, buffer_type=BufferType.VIDEO_CAPTURE, queue=True
    ):
        self._context_level = 0
        self.device = device
        self.index = index
        self.buffer_type = buffer_type
        self.queue = queue

    def _v4l2_buffer(self):
        buff = raw.v4l2_buffer()
        buff.index = self.index
        buff.type = self.buffer_type
        return buff

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def _ioctl(self, request, arg=0):
        return self.device._ioctl(request, arg=arg)

    def close(self):
        pass

class BufferMMAP(BaseBuffer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        buff = self._v4l2_buffer()
        self._ioctl(IOC.QUERYBUF, buff)
        self.mmap = mem_map(self.device, buff.length, offset=buff.m.offset)
        self.length = buff.length
        if self.queue:
            self._ioctl(IOC.QBUF, buff)

    def _v4l2_buffer(self):
        buff = super()._v4l2_buffer()
        buff.memory = Memory.MMAP
        return buff

    def close(self):
        if self.mmap is not None:
            self.mmap.close()
            self.mmap = None

    def raw_read(self, buff):
        result = self.mmap[: buff.bytesused]
        if self.queue:
            self._ioctl(IOC.QBUF, buff)
        return result

    def read(self, buff):
        select.select((self.device,), (), ())
        return self.raw_read(buff)


class Buffers:
    def __init__(
        self,
        device,
        buffer_type=BufferType.VIDEO_CAPTURE,
        buffer_size=1,
        memory=Memory.MMAP,
    ):
        self._context_level = 0
        self.device = device
        self.buffer_size = buffer_size
        self.buffer_type = buffer_type
        self.memory = memory
        self.buffers = self._create_buffers()

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def _ioctl(self, request, arg=0):
        return self.device._ioctl(request, arg=arg)

    def _create_buffers(self):
        if self.memory != Memory.MMAP:
            raise TypeError(f"Unsupported buffer type {self.memory.name!r}")
        r = request_buffers(
            self.device, self.buffer_type, self.memory, self.buffer_size
        )
        return [
            BufferMMAP(self.device, index, self.buffer_type) for index in range(r.count)
        ]

    def close(self):
        if self.buffers:
            for buff in self.buffers:
                buff.close()
            self.buffers = None
        r = raw.v4l2_requestbuffers()
        r.count = 0
        r.type = self.buffer_type
        r.memory = self.memory
        self._ioctl(IOC.REQBUFS, r)

    def raw_read(self):
        # ask which buffer is ready
        buff = self.buffers[0]._v4l2_buffer()
        self._ioctl(IOC.DQBUF, buff)
        return self.buffers[buff.index].raw_read(buff)

    def read(self):
        select.select((self.device,), (), ())
        return self.raw_read()


class VideoStream:
    def __init__(self, video_capture, buffer_size=1, memory=Memory.MMAP):
        self._context_level = 0
        self.video_capture = video_capture
        self.buffers = Buffers(
            video_capture.device, video_capture.buffer_type, buffer_size, memory
        )

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def __iter__(self):
        return Stream(self)

    async def __aiter__(self):
        async for frame in AsyncStream(self):
            yield frame

    def close(self):
        self.buffers.close()

    def raw_read(self):
        return self.buffers.raw_read()

    def read(self):
        return self.buffers.read()


def Stream(stream):
    stream.video_capture.stream_on()
    try:
        while True:
            yield stream.read()
    finally:
        stream.video_capture.stream_off()


async def AsyncStream(stream):
    import asyncio

    cap = stream.video_capture
    fd = cap.device.fileno()
    loop = asyncio.get_event_loop()
    event = asyncio.Event()
    loop.add_reader(fd, event.set)
    try:
        cap.stream_on()
        while True:
            await event.wait()
            event.clear()
            yield stream.raw_read()
    finally:
        cap.stream_off()
        loop.remove_reader(fd)


def iter_video_files(path="/dev"):
    path = pathlib.Path(path)
    return path.glob("video*")


def iter_devices(path="/dev"):
    return (Device(name) for name in iter_video_files(path=path))


def iter_video_capture_devices(path="/dev"):
    def filt(filename):
        with fopen(filename) as fobj:
            caps = read_capabilities(fobj.fileno())
            return Capability.VIDEO_CAPTURE in Capability(caps.device_caps)

    return (Device(name) for name in filter(filt, iter_video_files(path)))
