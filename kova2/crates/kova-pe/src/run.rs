use kova_image::verify;
use kova_image::volume_restore::{confirmation_phrase, restore_image_to_volume, volume_info};
use kova_pe::plan::*;
use std::fs::File;
use std::io::{BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::AtomicBool;

struct Log(Vec<String>);
impl Log {
    fn say(&mut self, s: impl AsRef<str>) {
        println!("{}", s.as_ref());
        self.0.push(s.as_ref().to_string());
    }
}

fn diskpart(script: &str) -> String {
    let dir = std::env::var("TEMP").unwrap_or_else(|_| "X:\\".into());
    let f = Path::new(&dir).join(format!("kova_dp_{}.txt", std::process::id()));
    let _ = std::fs::write(&f, script);
    let out = Command::new("diskpart").arg("/s").arg(&f).output();
    let _ = std::fs::remove_file(&f);
    match out {
        Ok(o) => String::from_utf8_lossy(&o.stdout).into_owned(),
        Err(e) => format!("diskpart failed: {e}"),
    }
}

fn exists_letter(l: char) -> bool {
    Path::new(&format!("{l}:\\")).exists()
}

fn find_images() -> Vec<PathBuf> {
    let mut v = Vec::new();
    for l in 'C'..='Z' {
        if l == 'X' {
            continue;
        }
        for sub in ["Kova2_Repository\\images", "KovaImages"] {
            let d = PathBuf::from(format!("{l}:\\{sub}"));
            if let Ok(rd) = std::fs::read_dir(&d) {
                for e in rd.flatten() {
                    let p = e.path();
                    if p.extension()
                        .is_some_and(|x| x.eq_ignore_ascii_case("qblk"))
                    {
                        v.push(p);
                    }
                }
            }
        }
    }
    v
}

type Auto = std::collections::HashMap<String, String>;

fn drive_of(p: &Path) -> Option<char> {
    p.to_string_lossy()
        .chars()
        .next()
        .map(|c| c.to_ascii_uppercase())
}

/// 자동 모드 파일은 이미지 폴더가 있는 드라이브의 루트에서만 찾는다(아무 USB·디스크 루트의 파일로 켜지지 않게).
fn read_auto(image_drives: &[char]) -> Option<(PathBuf, Auto)> {
    for l in image_drives {
        let p = PathBuf::from(format!("{l}:\\KOVA_AUTO.txt"));
        if let Ok(t) = std::fs::read_to_string(&p) {
            return Some((p, parse_auto(&t)));
        }
    }
    None
}

fn is_uefi() -> bool {
    Command::new("reg")
        .args([
            "query",
            "HKLM\\SYSTEM\\CurrentControlSet\\Control",
            "/v",
            "PEFirmwareType",
        ])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains("0x2"))
        .unwrap_or(true)
}

fn ask(prompt: &str) -> String {
    print!("{prompt}");
    let _ = std::io::stdout().flush();
    let mut s = String::new();
    let _ = std::io::stdin().read_line(&mut s);
    s.trim().to_string()
}

fn free_letters() -> Vec<char> {
    ['S', 'T', 'U', 'V', 'W', 'Y', 'Z']
        .into_iter()
        .filter(|l| !exists_letter(*l))
        .collect()
}

fn save_log(log: &Log, image: Option<&Path>) {
    let text = log.0.join("\r\n");
    let _ = std::fs::write("X:\\kova-pe.log", &text);
    if let Some(root) = image.and_then(|p| p.ancestors().last()) {
        let _ = std::fs::write(root.join("KOVA_PE_LOG.txt"), &text);
    }
}

fn finish(log: &mut Log, image: Option<&Path>, code: i32, auto: bool) -> i32 {
    log.say(if code == 0 {
        "RESULT: OK"
    } else {
        "RESULT: FAILED"
    });
    save_log(log, image);
    if auto {
        let _ = Command::new("wpeutil").arg("shutdown").status();
    } else {
        let _ = ask("Press Enter to reboot (on failure, Shift+F10 opens a command prompt).");
        let _ = Command::new("wpeutil").arg("reboot").status();
    }
    code
}

pub fn main() -> i32 {
    let mut log = Log(Vec::new());
    log.say("=== Kova2 WinPE system restore ===");

    // 1) 이미지 선택
    let images = find_images();
    if images.is_empty() {
        log.say("ERROR: no .qblk image found on any drive (Kova2_Repository\\images, KovaImages).");
        return finish(&mut log, None, 1, false);
    }
    let mut image_drives: Vec<char> = images.iter().filter_map(|p| drive_of(p)).collect();
    image_drives.sort();
    image_drives.dedup();
    let auto_file = read_auto(&image_drives);
    let auto = auto_file.as_ref().map(|(_, m)| m);
    let is_auto = auto.is_some();
    if let Some((p, _)) = &auto_file {
        log.say(format!("Auto mode file: {}", p.display()));
    }
    let image: PathBuf = if let Some(m) = auto {
        let want = m.get("image").cloned().unwrap_or_default().to_lowercase();
        // 자동 모드 이미지는 자동 모드 파일과 같은 드라이브에 있는 것만 고른다
        let auto_drive = auto_file.as_ref().and_then(|(p, _)| drive_of(p));
        let hit = images.iter().find(|p| {
            !want.is_empty()
                && drive_of(p) == auto_drive
                && p.to_string_lossy().to_lowercase().ends_with(&want)
        });
        match hit {
            Some(p) => p.clone(),
            None => {
                log.say(format!("ERROR: auto mode image={want} not found."));
                return finish(&mut log, None, 1, true);
            }
        }
    } else {
        for (i, p) in images.iter().enumerate() {
            let mb = std::fs::metadata(p).map(|m| m.len() >> 20).unwrap_or(0);
            println!("  [{}] {} ({} MB)", i + 1, p.display(), mb);
        }
        let n: usize = ask("Image number to restore: ").parse().unwrap_or(0);
        match images.get(n.wrapping_sub(1)) {
            Some(p) => p.clone(),
            None => {
                log.say("ERROR: invalid number");
                return finish(&mut log, None, 1, false);
            }
        }
    };
    log.say(format!("Image: {}", image.display()));

    // 2) 이미지 검증 (디스크를 건드리기 전)
    log.say("Verifying the whole image...");
    let verified = File::open(&image).map_err(|e| e.to_string()).and_then(|f| {
        verify(
            BufReader::with_capacity(8 << 20, f),
            &AtomicBool::new(false),
            &|_| {},
        )
        .map_err(|e| e.to_string())
    });
    let header = match verified {
        Ok((h, s)) => {
            log.say(format!(
                "Verify OK: clusters {} / used {} / chunks {}",
                h.total_clusters, h.used_clusters, s.chunks
            ));
            h
        }
        Err(e) => {
            log.say(format!(
                "ERROR: image verification failed, nothing was changed: {e}"
            ));
            return finish(&mut log, Some(&image), 1, is_auto);
        }
    };
    let image_bytes = header.total_clusters * header.cluster_size as u64;

    // 3) 대상 디스크
    let disks = parse_list_disk(&diskpart("list disk\nexit\n"));
    let img_letter = image.to_string_lossy().chars().next().unwrap_or('C');
    let image_disks = parse_detail_volume_disks(&diskpart(&script_detail_volume(img_letter)));
    if image_disks.is_empty() {
        log.say(format!(
            "ERROR: could not find which disk holds {img_letter}: (diskpart detail volume); nothing was changed."
        ));
        return finish(&mut log, Some(&image), 1, is_auto);
    }
    // 자동 모드는 디스크 고유 ID(disk_id)로만 지정한다. 번호(disk=)·크기(disk_mb=)는 거부한다.
    let auto_id: Option<String> = match auto.map(auto_disk_id) {
        None => None,
        Some(Ok(id)) => Some(id),
        Some(Err(e)) => {
            log.say(format!("ERROR: {e}. Nothing was changed."));
            return finish(&mut log, Some(&image), 1, true);
        }
    };
    let disk_num: u32 = if let Some(id) = &auto_id {
        // 디스크 고유 ID 가 정확히 하나의 디스크와 일치할 때만 선택한다.
        let hits: Vec<u32> = disks
            .iter()
            .filter(|d| {
                parse_disk_id(&diskpart(&format!(
                    "select disk {}
detail disk
exit
",
                    d.num
                )))
                .is_some_and(|x| x == *id)
            })
            .map(|d| d.num)
            .collect();
        if hits.len() == 1 {
            hits[0]
        } else {
            u32::MAX
        }
    } else {
        for d in &disks {
            let tag = if image_disks.contains(&d.num) {
                "  <- holds the image (cannot select)"
            } else {
                ""
            };
            println!(
                "  Disk {}: {} MB, {}{}",
                d.num,
                d.bytes >> 20,
                d.status,
                tag
            );
        }
        ask("Disk number to ERASE: ").parse().unwrap_or(u32::MAX)
    };
    let Some(disk) = disks.iter().find(|d| d.num == disk_num) else {
        log.say(format!("ERROR: disk {disk_num} not found"));
        return finish(&mut log, Some(&image), 1, is_auto);
    };
    if let Err(e) = disk_selected_ok(disk, &image_disks, image_bytes) {
        log.say(format!("ERROR: {e}"));
        return finish(&mut log, Some(&image), 1, is_auto);
    }
    let phrase = match &auto_id {
        Some(id) => confirm_phrase_id(id),
        None => confirm_phrase(disk.num),
    };
    let typed = if let Some(m) = auto {
        m.get("confirm").cloned().unwrap_or_default()
    } else {
        println!(
            "ALL data on disk {} ({} MB) will be erased.",
            disk.num,
            disk.bytes >> 20
        );
        ask(&format!("To continue type exactly [{phrase}]: "))
    };
    if norm(&typed) != phrase {
        log.say("ERROR: confirmation phrase does not match. Nothing was changed.");
        return finish(&mut log, Some(&image), 1, is_auto);
    }
    // 디스크를 건드리기 직전에 자동 모드 파일 이름을 바꾼다: 복원이 성공하든 실패·정전이든 재부팅마다 다시 지우지 않게.
    // 이름을 못 바꾸면 반복 삭제 위험이 있으므로 시작하지 않는다.
    if let Some((p, _)) = &auto_file {
        let done = p.with_file_name(AUTO_DONE_NAME);
        let _ = std::fs::remove_file(&done);
        if let Err(e) = std::fs::rename(p, &done) {
            log.say(format!(
                "ERROR: cannot rename {} (auto mode would repeat on every boot): {e}. Nothing was changed.",
                p.display()
            ));
            return finish(&mut log, Some(&image), 1, true);
        }
        log.say(format!("Auto mode file renamed to {}", done.display()));
    }

    // 4) 파티션
    let uefi = is_uefi();
    let fl = free_letters();
    if fl.len() < 2 {
        log.say("ERROR: not enough free drive letters.");
        return finish(&mut log, Some(&image), 1, is_auto);
    }
    let (sys, os) = (fl[0], fl[1]);
    log.say(format!(
        "Boot mode {}, system {sys}: target {os}: creating partitions",
        if uefi { "UEFI/GPT" } else { "BIOS/MBR" }
    ));
    let out = diskpart(&if uefi {
        script_gpt(disk.num, sys, os)
    } else {
        script_mbr(disk.num, sys, os)
    });
    if !exists_letter(os) || !exists_letter(sys) {
        log.say(format!("ERROR: partitioning failed\n{out}"));
        return finish(&mut log, Some(&image), 1, is_auto);
    }

    // 5) 복원
    let info = match volume_info(os) {
        Ok(i) => i,
        Err(e) => {
            log.say(format!("ERROR: target volume lookup failed: {e}"));
            return finish(&mut log, Some(&image), 1, is_auto);
        }
    };
    log.say(format!("Restore started -> {os}:"));
    let t = std::time::Instant::now();
    let res = restore_image_to_volume(
        &image,
        os,
        &confirmation_phrase(&info),
        &[],
        &AtomicBool::new(false),
        &|s| {
            if s.chunks % 200 == 0 {
                eprint!("\rchunks {} / {:.1} GB", s.chunks, s.raw_bytes as f64 / 1e9);
            }
        },
    );
    println!();
    if let Err(e) = res {
        log.say(format!("ERROR: restore failed: {e}"));
        return finish(&mut log, Some(&image), 1, is_auto);
    }
    log.say(format!("Restore done ({:.0}s)", t.elapsed().as_secs_f64()));

    // 6) 확장·부팅 구성
    let _ = diskpart(&script_extend(os));
    if !Path::new(&format!("{os}:\\Windows\\System32")).exists() {
        log.say(format!(
            "ERROR: {os}:\\Windows\\System32 is missing; the image seems to contain no Windows."
        ));
        return finish(&mut log, Some(&image), 1, is_auto);
    }
    match Command::new("bcdboot")
        .args(bcdboot_args(os, sys, uefi))
        .output()
    {
        Ok(o) if o.status.success() => log.say("bcdboot OK: boot files created."),
        Ok(o) => {
            log.say(format!(
                "ERROR: bcdboot failed: {}{}",
                String::from_utf8_lossy(&o.stdout),
                String::from_utf8_lossy(&o.stderr)
            ));
            return finish(&mut log, Some(&image), 1, is_auto);
        }
        Err(e) => {
            log.say(format!("ERROR: bcdboot could not run: {e}"));
            return finish(&mut log, Some(&image), 1, is_auto);
        }
    }
    finish(&mut log, Some(&image), 0, is_auto)
}
