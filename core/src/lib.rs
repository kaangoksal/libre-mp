//! libremp-core: epson protocol, capture, cast loop, wi-fi, qr, saved projectors

pub mod hex;
pub mod wifi;
pub mod capture;
#[cfg(target_os = "linux")]
pub mod screencast;
#[cfg(target_os = "linux")]
pub mod x11_cursor;
pub mod protocol;
pub mod config;
pub mod qr;
pub mod session;

use std::sync::atomic::{AtomicU32, Ordering};

// stream size sent to projector; 1920x1080 = 1:1 on a 1080p screen (capture scaling is nearest-neighbour). windows sends 1024x768
static STREAM_W: AtomicU32 = AtomicU32::new(1920);
static STREAM_H: AtomicU32 = AtomicU32::new(1080);

// stream width
pub fn stream_w() -> u32 {
    STREAM_W.load(Ordering::Relaxed)
}

// stream height
pub fn stream_h() -> u32 {
    STREAM_H.load(Ordering::Relaxed)
}

// pick stream size before casting; even sizes only (4:2:0 jpeg)
pub fn set_stream_size(w: u32, h: u32) {
    STREAM_W.store(w.clamp(64, 4096) & !1, Ordering::Relaxed);
    STREAM_H.store(h.clamp(64, 4096) & !1, Ordering::Relaxed);
}
// jpeg quality before per-tile step-down
pub const JPEG_QUALITY: i32 = 95;
