const std = @import("std");

const HMODULE = ?*anyopaque;
const BOOL = i32;
const DWORD = u32;

extern "kernel32" fn LoadLibraryA(path: [*:0]const u8) callconv(.winapi) HMODULE;
extern "kernel32" fn FreeLibrary(module: HMODULE) callconv(.winapi) BOOL;
extern "kernel32" fn Sleep(milliseconds: DWORD) callconv(.winapi) void;

const ConnectCb = *const fn (u32) callconv(.winapi) void;
const RecvCb = *const fn (u32, [*:0]const u8, u32) callconv(.winapi) void;
const CloseCb = *const fn (u32) callconv(.winapi) void;

const UseUtf8 = *const fn () callconv(.winapi) void;
const InitWxWorkSocket = *const fn (ConnectCb, RecvCb, CloseCb) callconv(.winapi) i32;
const InjectWxWork = *const fn ([*:0]const u8, [*:0]const u8) callconv(.winapi) i32;
const DestroyWxWork = *const fn () callconv(.winapi) i32;

const OFFSET_USE_UTF8 = 0x4A60;
const OFFSET_INIT_SOCKET = 0x4B10;
const OFFSET_INJECT = 0x4BF0;
const OFFSET_DESTROY = 0x5310;

fn onConnect(cid: u32) callconv(.winapi) void {
    std.debug.print("[ZIG CONNECTED] ID={d}\n", .{cid});
}

fn onRecv(cid: u32, msg: [*:0]const u8, len: u32) callconv(.winapi) void {
    _ = len;
    const text = std.mem.span(msg);
    std.debug.print("[ZIG RECEIVED] ID={d}\n", .{cid});
    std.debug.print("[ZIG RECEIVED MSG] {s}\n", .{text});
}

fn onClose(cid: u32) callconv(.winapi) void {
    std.debug.print("[ZIG DISCONNECTED] ID={d}\n", .{cid});
}

fn ptrAt(comptime T: type, module: HMODULE, offset: usize) T {
    const base: usize = @intFromPtr(module.?);
    return @ptrFromInt(base + offset);
}

pub fn main(init: std.process.Init) !void {
    var args = try std.process.Args.Iterator.initAllocator(init.minimal.args, init.gpa);
    defer args.deinit();

    _ = args.next();
    const dll_path = args.next() orelse "loader.dll";
    const helper_path = args.next() orelse "helper.dll";
    const wxwork_path = args.next() orelse "C:\\Program Files (x86)\\WXWork\\WXWork.exe";

    var dll_buf: [std.fs.max_path_bytes:0]u8 = undefined;
    var helper_buf: [std.fs.max_path_bytes:0]u8 = undefined;
    var wxwork_buf: [std.fs.max_path_bytes:0]u8 = undefined;

    const dll_z = try std.fmt.bufPrintZ(&dll_buf, "{s}", .{dll_path});
    const helper_z = try std.fmt.bufPrintZ(&helper_buf, "{s}", .{helper_path});
    const wxwork_z = try std.fmt.bufPrintZ(&wxwork_buf, "{s}", .{wxwork_path});

    const module = LoadLibraryA(dll_z.ptr);
    if (module == null) {
        std.debug.print("[ZIG FATAL] Failed to load DLL: {s}\n", .{dll_path});
        return error.LoadDllFailed;
    }
    defer _ = FreeLibrary(module);

    const use_utf8 = ptrAt(UseUtf8, module, OFFSET_USE_UTF8);
    const init_socket = ptrAt(InitWxWorkSocket, module, OFFSET_INIT_SOCKET);
    const inject = ptrAt(InjectWxWork, module, OFFSET_INJECT);
    const destroy = ptrAt(DestroyWxWork, module, OFFSET_DESTROY);

    use_utf8();
    _ = init_socket(onConnect, onRecv, onClose);

    const inject_res = inject(helper_z.ptr, wxwork_z.ptr);
    std.debug.print("[ZIG INJECT RESULT] {d}\n", .{inject_res});
    std.debug.print("[ZIG WAITING] listening for callbacks for 60 seconds\n", .{});

    Sleep(60_000);
    _ = destroy();
}
