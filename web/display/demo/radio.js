// Demo radio (?demo=1): a made-up station schedule for the on-screen radio panel — tracks change, stations switch
// with a short "connecting" phase, one station sends no track titles. Names and titles are fictional.
// Pure: status(t) → Telemetry.radio (docs/contracts/radio.md, section 5).

export const DEMO_STATIONS = [
  {
    name: "Wasteland FM",
    genre: "Oldies",
    tracks: [
      "The Velvet Geigers - Glow Little Desert Moon",
      "Sally Chrome - Atomic Sunset",
      "Dusty Ray & The Fallouts - Six Feet of Sand",
      "The Neon Mirage Orchestra - Strip Lights at Midnight (Extended Wasteland Mix)",
    ],
  },
  { name: "Mojave Wave 101", genre: "Rock", tracks: ["Iron Coyote - Highway of Dust", "Rad Roach - Bunker Blues"] },
  { name: "Rust Belt Radio", genre: "Jazz", tracks: [] }, // no StreamTitle: the panel shows the genre
];

export const TRACK_S = 24; // one "song"
export const STATION_S = 150; // a station plays this long, then the demo tunes to the next one
export const CONNECT_S = 3; // tuning to a new station

/** Radio status at demo time t (seconds since start). */
export function demoRadioStatus(t, { volume = 60, output = "Wasteland Boombox" } = {}) {
  const tt = Math.max(0, t);
  const k = Math.floor(tt / STATION_S);
  const st = DEMO_STATIONS[k % DEMO_STATIONS.length];
  const inStation = tt - k * STATION_S;
  const connecting = k > 0 && inStation < CONNECT_S;
  const n = st.tracks.length;
  const track = connecting || !n ? null : st.tracks[Math.floor(Math.max(0, inStation - (k > 0 ? CONNECT_S : 0)) / TRACK_S) % n];
  return {
    state: connecting ? "connecting" : "playing",
    station: st.name,
    track,
    genre: st.genre,
    volume,
    output,
    output_connected: true,
  };
}
