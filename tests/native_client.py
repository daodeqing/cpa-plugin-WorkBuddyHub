"""在隔离目录加载真实 DLL；不读取用户账号。"""
import ctypes as C
import json


class Buffer(C.Structure):
    _fields_ = [("ptr", C.c_void_p), ("len", C.c_size_t)]


HOSTCALL = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_void_p, C.c_size_t, C.POINTER(Buffer))
FREE = C.CFUNCTYPE(None, C.c_void_p, C.c_size_t)
CALL = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_void_p, C.c_size_t, C.POINTER(Buffer))
SHUTDOWN = C.CFUNCTYPE(None)


class HostApi(C.Structure):
    _fields_ = [("version", C.c_uint32), ("context", C.c_void_p), ("call", HOSTCALL), ("free_buffer", FREE)]


class PluginApi(C.Structure):
    _fields_ = [("version", C.c_uint32), ("call", CALL), ("free_buffer", FREE), ("shutdown", SHUTDOWN)]


class NativeClient:
    def __init__(self, library):
        self.buffers = {}
        self.events = []
        def hostcall(ctx, name, ptr, length, out):
            req = json.loads(C.string_at(ptr, length))
            self.events.append((name.decode(), req))
            raw = C.create_string_buffer(b'{"ok":true,"result":{}}')
            address = C.addressof(raw)
            self.buffers[address] = raw
            out[0].ptr = address
            out[0].len = len(raw.value)
            return 0
        def free(ptr, length):
            self.buffers.pop(ptr, None)
        self.callback = HOSTCALL(hostcall)
        self.free_callback = FREE(free)
        self.host = HostApi(1, None, self.callback, self.free_callback)
        self.api = PluginApi()
        self.dll = C.CDLL(str(library))
        self.dll.cliproxy_plugin_init.argtypes = [C.POINTER(HostApi), C.POINTER(PluginApi)]
        if self.dll.cliproxy_plugin_init(C.byref(self.host), C.byref(self.api)):
            raise RuntimeError("native init failed")

    def call(self, method, req=None):
        raw = json.dumps(req or {}).encode()
        buf = Buffer()
        rc = self.api.call(method.encode(), raw, len(raw), C.byref(buf))
        try:
            value = json.loads(C.string_at(buf.ptr, buf.len))
        finally:
            self.api.free_buffer(buf.ptr, buf.len)
        if rc or not value.get("ok"):
            raise RuntimeError(json.dumps(value, ensure_ascii=False))
        return value["result"]

    def close(self):
        self.api.shutdown()
