#[cfg(not(target_os = "windows"))]
compile_error!("This binary only builds on Windows targets.");

use std::{
    env,
    ffi::{c_char, CString},
    fs::File,
    io::{BufRead, BufReader},
    process,
    sync::{
        atomic::{AtomicBool, AtomicU32, Ordering},
        OnceLock,
    },
    thread,
    time::Duration,
};

use windows_sys::Win32::{
    Foundation::HMODULE,
    System::{
        Console::{SetConsoleCtrlHandler, CTRL_C_EVENT, CTRL_CLOSE_EVENT},
        LibraryLoader::LoadLibraryA,
        Threading::Sleep,
    },
};

#[cfg(target_os = "windows")]
#[link(name = "kernel32")]
extern "system" {
    fn FreeLibrary(h_lib_module: HMODULE) -> i32;
}

type ConnectCb = unsafe extern "system" fn(u32);
type RecvCb = unsafe extern "system" fn(u32, *const c_char, u32);
type CloseCb = unsafe extern "system" fn(u32);

type UseUtf8 = Option<unsafe extern "system" fn()>;
type InitWxWorkSocket = Option<unsafe extern "system" fn(ConnectCb, RecvCb, CloseCb) -> i32>;
type InjectWxWork = Option<unsafe extern "system" fn(*const c_char, *const c_char) -> i32>;
type DestroyWxWork = Option<unsafe extern "system" fn() -> i32>;
type SendWxWorkData = Option<unsafe extern "system" fn(u32, *const c_char) -> i32>;

const OFFSET_USE_UTF8: usize = 0x4A60;
const OFFSET_INIT_SOCKET: usize = 0x4B10;
const OFFSET_INJECT: usize = 0x4BF0;
const OFFSET_DESTROY: usize = 0x5310;
const OFFSET_SEND_DATA: usize = 0x5800;
const POLL_INTERVAL_MS: u64 = 1000;
const DEFAULT_HOOK: &str = "http://localhost:6255/message";

#[derive(Clone, Copy, Debug)]
struct FunctionPointers {
    dll_handle: HMODULE,
    use_utf8: UseUtf8,
    init_socket: InitWxWorkSocket,
    inject: InjectWxWork,
    destroy: DestroyWxWork,
    send_data: SendWxWorkData,
}

static HOOK_URL: OnceLock<String> = OnceLock::new();
static FUNCTIONS: OnceLock<FunctionPointers> = OnceLock::new();
static EXIT_FLAG: AtomicBool = AtomicBool::new(false);
static CLIENT_ID: AtomicU32 = AtomicU32::new(0);

fn log_info(msg: &str) {
    println!("{msg}");
}

fn read_ini_config(path: &str) -> Option<String> {
    let file = File::open(path).ok()?;
    let reader = BufReader::new(file);
    let mut in_config = false;
    for line in reader.lines() {
        let line = line.ok()?;
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with(';') || trimmed.starts_with('#') {
            continue;
        }
        if !in_config {
            if trimmed.eq_ignore_ascii_case("[config]") {
                in_config = true;
            }
            continue;
        }
        if trimmed.starts_with('[') {
            break;
        }
        if let Some((key, value)) = trimmed.split_once('=') {
            let key = key.trim();
            let value = value.trim();
            if key.eq_ignore_ascii_case("hook") {
                return Some(value.to_string());
            }
        }
    }
    None
}

fn to_cstring(value: &str, label: &str) -> CString {
    CString::new(value).unwrap_or_else(|_| {
        eprintln!("[FATAL] {label} contains interior NUL bytes");
        process::exit(1);
    })
}

fn load_dll_and_resolve(dll_path: &str) -> FunctionPointers {
    let dll_cstr = to_cstring(dll_path, "dll_path");
    let handle = unsafe { LoadLibraryA(dll_cstr.as_ptr() as *const u8) };
    if handle == 0 {
        eprintln!("[FATAL] Failed to load DLL: {dll_path}");
        process::exit(1);
    }

    let base = handle as usize;
    FunctionPointers {
        dll_handle: handle,
        use_utf8: Some(unsafe { std::mem::transmute(base + OFFSET_USE_UTF8) }),
        init_socket: Some(unsafe { std::mem::transmute(base + OFFSET_INIT_SOCKET) }),
        inject: Some(unsafe { std::mem::transmute(base + OFFSET_INJECT) }),
        destroy: Some(unsafe { std::mem::transmute(base + OFFSET_DESTROY) }),
        send_data: Some(unsafe { std::mem::transmute(base + OFFSET_SEND_DATA) }),
    }
}

fn report_to_server(_cid: u32, json: &str) {
    let Some(url) = HOOK_URL.get() else { return };
    let response = ureq::post(url)
        .set("Content-Type", "application/json")
        .send_string(json);
    if let Err(err) = response {
        log_info(&format!("[CURL ERROR] {err}"));
    }
}

fn send_wxwork_data(body: &str) {
    let cid = CLIENT_ID.load(Ordering::SeqCst);
    let Some(fns) = FUNCTIONS.get() else {
        log_info("[ERROR] Function pointers not initialized");
        return;
    };
    let Some(sender) = fns.send_data else {
        log_info("[ERROR] SendWxWorkData pointer missing");
        return;
    };
    if cid == 0 {
        log_info("[ERROR] g_client_id is 0");
        return;
    }
    match CString::new(body) {
        Ok(msg) => unsafe {
            let rc = sender(cid, msg.as_ptr());
            if rc != 1 {
                log_info(&format!("[ERROR] SendWxWorkData returned: {rc}"));
            }
        },
        Err(_) => log_info("[ERROR] Payload contains interior NUL byte"),
    }
}

fn polling_loop() {
    let Some(url) = HOOK_URL.get().cloned() else {
        log_info("[FATAL] hook not configured");
        return;
    };

    log_info(&format!(
        "[POLLING] Interval: {} ms to {url}",
        POLL_INTERVAL_MS
    ));

    while !EXIT_FLAG.load(Ordering::SeqCst) {
        match ureq::get(&url).call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => {
                    send_wxwork_data(&body);
                    log_info(&format!("[POLL RESPONSE] {body}"));
                }
                Err(err) => log_info(&format!("[POLL ERROR] Failed to read body: {err}")),
            },
            Err(err) => log_info(&format!("[POLL ERROR] {err}")),
        }
        thread::sleep(Duration::from_millis(POLL_INTERVAL_MS));
    }
}

unsafe extern "system" fn on_connect(cid: u32) {
    CLIENT_ID.store(cid, Ordering::SeqCst);
    log_info(&format!("[CONNECTED] ID={cid}"));
}

unsafe extern "system" fn on_recv(cid: u32, msg: *const c_char, _len: u32) {
    if msg.is_null() {
        log_info("[RECEIVED] null pointer");
        return;
    }
    let cstr = unsafe { std::ffi::CStr::from_ptr(msg) };
    if let Ok(text) = cstr.to_str() {
        log_info(&format!("[RECEIVED] ID={cid}"));
        log_info(&format!("[RECEIVED MSG] {text}"));
        report_to_server(cid, text);
    } else {
        log_info("[RECEIVED] Non UTF-8 payload");
    }
}

unsafe extern "system" fn on_close(cid: u32) {
    log_info(&format!("[DISCONNECTED] ID={cid}"));
}

unsafe extern "system" fn ctrl_handler(ctrl_type: u32) -> i32 {
    if ctrl_type == CTRL_C_EVENT || ctrl_type == CTRL_CLOSE_EVENT {
        EXIT_FLAG.store(true, Ordering::SeqCst);
        return 1;
    }
    0
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let dll_path = args.get(1).map(String::as_str).unwrap_or("loader.dll");
    let helper_path = args.get(2).map(String::as_str).unwrap_or("helper.dll");
    let wxwork_path = args
        .get(3)
        .map(String::as_str)
        .unwrap_or(r"C:\Program Files (x86)\WXWork\WXWork.exe");

    unsafe {
        SetConsoleCtrlHandler(Some(ctrl_handler), 1);
    }

    let hook = read_ini_config("bot.ini").unwrap_or_else(|| {
        log_info("[INFO] Using default hook URL");
        DEFAULT_HOOK.to_string()
    });
    HOOK_URL.set(hook).expect("HOOK_URL already set");

    let fns = load_dll_and_resolve(dll_path);
    FUNCTIONS.set(fns).expect("FUNCTIONS already set");

    unsafe {
        if let Some(use_utf8) = fns.use_utf8 {
            use_utf8();
        }
        if let Some(init) = fns.init_socket {
            init(on_connect, on_recv, on_close);
        }
    }

    let helper_c = to_cstring(helper_path, "helper_path");
    let wxwork_c = to_cstring(wxwork_path, "wxwork_path");

    let inject_res = unsafe {
        fns.inject
            .map(|f| f(helper_c.as_ptr(), wxwork_c.as_ptr()))
            .unwrap_or(-1)
    };
    log_info(&format!("[INJECT RESULT] {inject_res}"));

    polling_loop();

    unsafe {
        if let Some(destroy) = fns.destroy {
            destroy();
        }
        if fns.dll_handle != 0 {
            FreeLibrary(fns.dll_handle);
        }
        Sleep(500);
    }
}
