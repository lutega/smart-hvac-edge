// Casing panel dinding — LCD 7" HDMI sentuh 1024x600 + Raspberry Pi Zero 2 W
// Smart HVAC ruang rapat (KS5102). Bahan: PETG, nozzle 0.4, layer 0.2, 4 dinding, infill 25 %.
//
// UKUR LCD ANDA DULU dan sesuaikan 6 angka bertanda (*) di bawah. Nilai bawaan = modul
// 7" kapasitif 1024x600 dengan kaca 164,9 x 100 mm (tipe paling umum, mis. Waveshare 7inch HDMI LCD (C)).
//
// Bagian:  part = "front"  -> bingkai depan + kotak (cetak muka layar menghadap bed)
//          part = "back"   -> pelat belakang / dudukan dinding (cetak sisi datar menghadap bed)
//          part = "assembly" -> pratinjau rakitan
// Ekspor:  openscad -D 'part="front"' -o front.stl panel_dinding_lcd7.scad

part = "assembly";

/* ---------- LCD (ukur dengan jangka sorong) ---------- */
lcd_w      = 164.9;   // (*) lebar kaca/modul paling luar
lcd_h      = 100.0;   // (*) tinggi kaca/modul paling luar
lcd_t      = 6.0;     // (*) tebal kaca + panel di tepi (tanpa PCB driver)
view_w     = 155.0;   // (*) lebar area gambar yang menyala
view_h     = 87.0;    // (*) tinggi area gambar yang menyala
view_dx    = 0.0;     // (*) geser area gambar dari tengah kaca (+ = kanan, dilihat dari depan)
view_dy    = 0.0;     //     geser vertikal (+ = atas)
back_depth = 24.0;    // ruang di belakang panel: PCB driver + Pi Zero + kabel HDMI/USB

/* ---------- casing ---------- */
clr     = 0.4;   // celah pas LCD
side    = 4.0;   // tebal tepi kiri/kanan di luar LCD
top     = 10.0;  // tepi atas (berisi boss sekrup)
chin    = 14.0;  // tepi bawah
skin    = 2.0;   // tebal muka depan
wall    = 2.4;   // tebal dinding kotak
r_out   = 6.0;   // radius sudut
plate_t = 3.0;   // tebal pelat belakang
lip     = 2.0;   // bibir pelat yang masuk ke kotak
foam    = 1.5;   // busa/double tape antara tiang penekan dan punggung LCD

pocket_w = lcd_w + 2*clr;
pocket_h = lcd_h + 2*clr;
W = pocket_w + 2*side;
H = pocket_h + top + chin;
D = skin + lcd_t + back_depth;               // kedalaman kotak depan
boss_x = pocket_w/2 - 16;
boss_y_top = pocket_h/2 + top/2;             // diukur dari pusat LCD
boss_y_bot = -(pocket_h/2 + chin/2);
bosses = [[ boss_x, boss_y_top], [-boss_x, boss_y_top], [ boss_x, boss_y_bot], [-boss_x, boss_y_bot]];

$fn = 48;

module rrect(w, h, r, t) { linear_extrude(t) offset(r) square([w - 2*r, h - 2*r], center = true); }

// semua koordinat XY relatif pusat LCD; casing digeser oleh offset vertikal
off_y = (chin - top) / 2;   // pusat casing ada di y = -off_y relatif pusat LCD

module front() {
  difference() {
    union() {
      difference() {
        translate([0, -off_y, 0]) rrect(W, H, r_out, D);
        // rongga dalam
        translate([0, -off_y, skin]) rrect(W - 2*wall, H - 2*wall, r_out - wall, D);
      }
      // isi rongga sampai belakang LCD + 2 mm -> dinding kantong yang memegang LCD rapat
      translate([0, -off_y, 0]) rrect(W - 2*wall + 0.02, H - 2*wall + 0.02, r_out - wall, skin + lcd_t + 2);
      // boss sekrup
      for (b = bosses) translate([b[0], b[1], 0]) cylinder(d = 8, h = D);
    }
    // jendela layar dengan chamfer 45° ke depan
    translate([view_dx, view_dy, -0.01])
      hull() {
        linear_extrude(0.01) square([view_w + 2*skin, view_h + 2*skin], center = true);
        translate([0, 0, skin]) linear_extrude(0.02) square([view_w, view_h], center = true);
      }
    translate([view_dx, view_dy, -1]) linear_extrude(skin + 2) square([view_w, view_h], center = true);
    // kantong LCD
    translate([0, 0, skin]) linear_extrude(D) square([pocket_w, pocket_h], center = true);
    // lubang sekrup M3 (self-tapping 2,6 mm) dari belakang
    for (b = bosses) translate([b[0], b[1], D - 12]) cylinder(d = 2.6, h = 13);
    // ventilasi atas & bawah (dekat belakang)
    for (i = [-3:3]) {
      translate([i*14, -off_y + H/2 - wall/2, D - back_depth/2]) cube([8, wall + 2, 2.2], center = true);
      translate([i*14 + 7, -off_y - H/2 + wall/2, D - back_depth/2]) cube([8, wall + 2, 2.2], center = true);
    }
    // takik kabel di dinding bawah (kabel USB menempel tembok)
    translate([pocket_w/2 - 30, -off_y - H/2, D - 4]) cube([14, 2*wall + 2, 8.01], center = true);
  }
}

module back() {
  difference() {
    union() {
      translate([0, -off_y, 0]) rrect(W, H, r_out, plate_t);
      // bibir pengarah yang masuk ke kotak
      difference() {
        translate([0, -off_y, plate_t]) rrect(W - 2*wall - 2*clr, H - 2*wall - 2*clr, r_out - wall, lip);
        translate([0, -off_y, plate_t - 0.1]) rrect(W - 2*wall - 2*clr - 4, H - 2*wall - 2*clr - 4, r_out - wall - 2, lip + 1);
        for (b = bosses) translate([b[0], b[1], plate_t - 0.1]) cylinder(d = 9, h = lip + 1);   // celah untuk boss
      }
      // tiang penekan LCD di 4 sudut kaca (pakai busa tipis)
      for (sx = [-1, 1], sy = [-1, 1])
        translate([sx*(lcd_w/2 - 7), sy*(lcd_h/2 - 7), plate_t])
          cylinder(d = 8, h = back_depth - foam);
      // dudukan Raspberry Pi Zero 2 W (lubang 58 x 23 mm, M2.5) — kiri bawah
      for (dx = [0, 58], dy = [0, 23])
        translate([-lcd_w/2 + 14 + dx, -lcd_h/2 + 14 + dy, plate_t]) cylinder(d = 5.5, h = 5);
    }
    // lubang sekrup ke boss (M3, kepala benam)
    for (b = bosses) translate([b[0], b[1], -0.01]) {
      cylinder(d = 3.4, h = plate_t + 1);
      cylinder(d1 = 6.5, d2 = 3.4, h = 1.6);
    }
    // keyhole gantung dinding: kepala sekrup 8 mm, batang 4 mm
    for (sx = [-1, 1]) translate([sx*50, 22, -0.01]) {
      cylinder(d = 8.5, h = plate_t + 1);
      translate([0, 5, 0]) cylinder(d = 4.4, h = plate_t + 1);
      translate([-2.2, 0, 0]) cube([4.4, 5, plate_t + 1]);
    }
    // lubang kabel lewat tembok (kotak inbow / kabel tanam)
    translate([25, -10, -0.01]) cylinder(d = 14, h = plate_t + 1);
    // ventilasi pelat
    for (i = [0:5]) translate([5 + i*7, 20, -0.01]) linear_extrude(plate_t + 1) offset(1) square([0.2, 26], center = true);
    // lubang sekrup Pi
    for (dx = [0, 58], dy = [0, 23])
      translate([-lcd_w/2 + 14 + dx, -lcd_h/2 + 14 + dy, -0.01]) cylinder(d = 2.3, h = plate_t + 6);
  }
}

if (part == "front") front();
if (part == "back") back();
if (part == "assembly") {
  color("#2c3e50") front();
  color("#7f8c8d") translate([0, 0, D + plate_t + 0.2]) mirror([0, 0, 1]) back();
}
