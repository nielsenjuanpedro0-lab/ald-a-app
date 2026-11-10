// Puente con el backend Python: la ventana y la navegación son Rust/HTML,
// pero el Excel, los PDF y WhatsApp los sigue resolviendo el mismo código
// Python ya probado (ver backend/server.py). Rust solo lo arranca una vez
// y le habla por stdin/stdout, una línea JSON por pedido.
//
// El envío por WhatsApp puede tardar minutos (sesión + decenas de mensajes),
// y durante ese tiempo el backend va mandando líneas de progreso sueltas
// (sin pedirlas) además de la respuesta final. Por eso un hilo aparte lee
// stdout todo el tiempo: las líneas con "id" van al pedido que las espera,
// las líneas con "event" se reenvían a la interfaz como evento de Tauri.
use serde_json::Value;
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, State};

type Pendientes = Arc<Mutex<HashMap<u64, Sender<Value>>>>;

struct Backend {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    next_id: Mutex<u64>,
    pendientes: Pendientes,
}

impl Backend {
    fn new() -> Self {
        Self {
            child: Mutex::new(None),
            stdin: Mutex::new(None),
            next_id: Mutex::new(0),
            pendientes: Arc::new(Mutex::new(HashMap::new())),
        }
    }
}

// En desarrollo corre "python server.py" (el sistema ya tiene todo
// instalado, cómodo para iterar). Empaquetado no se puede asumir que la PC
// destino tenga Python ni pandas/playwright instalados, así que ahí se usa
// el backend ya congelado con PyInstaller (ver backend/build_exe.ps1):
// un .exe propio que no depende de nada afuera.
enum BackendKind {
    DevScript(std::path::PathBuf),
    PackagedExe(std::path::PathBuf),
}

fn backend_target(app: &tauri::AppHandle) -> Result<BackendKind, String> {
    // `cargo run` siempre arranca con CARGO_MANIFEST_DIR apuntando a
    // src-tauri/, así que el backend está siempre un nivel arriba, sin
    // importar desde dónde se haya lanzado el proceso.
    let dev_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../backend/server.py");
    if dev_path.exists() {
        return Ok(BackendKind::DevScript(dev_path));
    }
    // Empaquetado: el instalador deja backend/server.exe al lado del
    // ejecutable (ver tauri.conf.json -> bundle.resources).
    app.path()
        .resolve("backend/server.exe", tauri::path::BaseDirectory::Resource)
        .map(BackendKind::PackagedExe)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn backend_start(app: tauri::AppHandle, state: State<Backend>) -> Result<(), String> {
    let mut child_lock = state.child.lock().map_err(|e| e.to_string())?;
    if child_lock.is_some() {
        return Ok(()); // ya está arriba
    }

    let mut cmd = match backend_target(&app)? {
        BackendKind::DevScript(script) => {
            let mut c = Command::new("python");
            c.arg(script);
            c
        }
        BackendKind::PackagedExe(exe) => Command::new(exe),
    };
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = cmd.spawn().map_err(|e| format!("No se pudo iniciar el backend Python: {e}"))?;
    let stdin = child.stdin.take().ok_or("sin stdin")?;
    let stdout = child.stdout.take().ok_or("sin stdout")?;

    let pendientes = state.pendientes.clone();
    let app_handle: AppHandle = app.clone();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut linea = String::new();
        loop {
            linea.clear();
            match reader.read_line(&mut linea) {
                Ok(0) => break, // el proceso Python terminó
                Ok(_) => {
                    let Ok(valor) = serde_json::from_str::<Value>(linea.trim()) else { continue };
                    if let Some(id) = valor.get("id").and_then(Value::as_u64) {
                        if let Some(tx) = pendientes.lock().unwrap().remove(&id) {
                            let _ = tx.send(valor);
                        }
                    } else if valor.get("event").is_some() {
                        let _ = app_handle.emit("backend-event", valor);
                    }
                }
                Err(_) => break,
            }
        }
        // El backend murió: a cualquiera que siga esperando una respuesta,
        // que no se quede colgado hasta el timeout de 15 minutos.
        for (_, tx) in pendientes.lock().unwrap().drain() {
            let _ = tx.send(serde_json::json!({"ok": false, "error": "el backend Python se cerró inesperadamente"}));
        }
    });

    *state.stdin.lock().map_err(|e| e.to_string())? = Some(stdin);
    *child_lock = Some(child);
    Ok(())
}

#[tauri::command]
async fn backend_call(state: State<'_, Backend>, cmd: String, args: Value) -> Result<Value, String> {
    let id = {
        let mut n = state.next_id.lock().map_err(|e| e.to_string())?;
        *n += 1;
        *n
    };

    let (tx, rx) = channel::<Value>();
    state.pendientes.lock().map_err(|e| e.to_string())?.insert(id, tx);

    let pedido = serde_json::json!({ "id": id, "cmd": cmd, "args": args });
    let linea = serde_json::to_string(&pedido).map_err(|e| e.to_string())? + "\n";
    {
        let mut stdin_lock = state.stdin.lock().map_err(|e| e.to_string())?;
        let stdin = stdin_lock.as_mut().ok_or("el backend no está corriendo")?;
        stdin
            .write_all(linea.as_bytes())
            .map_err(|e| format!("no pude escribirle al backend: {e}"))?;
        stdin.flush().map_err(|e| e.to_string())?;
    }

    // El envío por WhatsApp puede tardar bastante (QR + decenas de mensajes):
    // 15 minutos de margen antes de darlo por colgado. Antes este recv()
    // bloqueaba el comando de forma sincrónica, y como los comandos de Tauri
    // corren sobre el mismo hilo que la ventana, la app entera se quedaba
    // "sin responder" durante toda la espera. spawn_blocking lo saca a un
    // hilo aparte para que la ventana siga viva mientras tanto.
    let valor = tauri::async_runtime::spawn_blocking(move || {
        rx.recv_timeout(Duration::from_secs(900))
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|_| "el backend no respondió a tiempo".to_string())?;

    if valor.get("ok").and_then(Value::as_bool) == Some(true) {
        Ok(valor.get("data").cloned().unwrap_or(Value::Null))
    } else {
        let msg = valor
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("error desconocido del backend");
        Err(msg.to_string())
    }
}

#[tauri::command]
fn backend_cancel(app: tauri::AppHandle, state: State<Backend>) -> Result<(), String> {
    // Cancelar una conexión de WhatsApp a medio escanear: no hay forma de
    // interrumpir solo esa espera sin tocar el resto, así que se mata el
    // proceso Python entero (lo que corta el QR en seco, como queremos) y
    // se lo levanta de nuevo para que el resto de la app siga funcionando.
    {
        let mut child_lock = state.child.lock().map_err(|e| e.to_string())?;
        if let Some(child) = child_lock.as_mut() {
            let _ = child.kill();
        }
        *child_lock = None;
    }
    *state.stdin.lock().map_err(|e| e.to_string())? = None;
    backend_start(app, state)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(Backend::new())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![backend_start, backend_call, backend_cancel])
        .setup(|app| {
            let handle = app.handle().clone();
            let state: State<Backend> = handle.state();
            if let Err(e) = backend_start(handle.clone(), state) {
                eprintln!("No se pudo levantar el backend Python al arrancar: {e}");
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Si el backend queda esperando algo de WhatsApp (QR, sync) cuando
            // se cierra la ventana, el proceso Python sigue vivo de fondo y
            // deja el perfil de Chrome bloqueado para el próximo arranque.
            // Lo matamos explícitamente en vez de esperar a que note el EOF.
            if let tauri::RunEvent::Exit = event {
                let state: State<Backend> = app_handle.state();
                if let Ok(mut child_lock) = state.child.lock() {
                    if let Some(child) = child_lock.as_mut() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
