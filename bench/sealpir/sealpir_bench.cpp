// Benchmark driver for Microsoft SealPIR (master cf55f10) on Microsoft SEAL 4.0.0.
//
// Modes (one configuration per process; one JSON object per trial is appended to --out):
//   block  SealPIR as-is: one PIR retrieval of one element (configs A and C).
//          Also times SealPIR's public PIRServer::expand_query alone and, for d = 1, a
//          "reuse" reply that multiplies an already expanded NTT-form query with the same
//          SealPIR-encoded plaintexts (SealPIR's own inner loop, without re-expansion).
//   track  Whole-track retrieval (config B). SealPIR elements cannot exceed one plaintext,
//          so each track is cut into chunks of --chunk bytes; block database j holds chunk j
//          of every track (R items) and ONE query is generated and reused for all blocks.
//            --variant api    one PIRServer per block database, generate_reply per block
//                             (SealPIR re-expands the query for every block);
//            --variant reuse  SealPIR's expand_query once per retrieval, then SealPIR's
//                             inner loop (multiply_plain/add, inverse NTT, mod switch) per block.
//   probe  Calls gen_pir_params (and optionally PIRServer::set_database) for an element size,
//          to document SealPIR's element-size limit. Run it in its own process.
//   instances  Memory cost of keeping many block databases alive with SealPIR's API: creates
//          --instances PIRServer objects (each with its own SEALContext, a copy of the Galois
//          keys and an R-item preprocessed database) and samples the resident set size.
//
// All SealPIR prints to std::cout are discarded (cout is redirected); progress goes to stderr.
#include "pir.hpp"
#include "pir_client.hpp"
#include "pir_server.hpp"

#include <seal/seal.h>
#include <sys/resource.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#ifndef SEALPIR_COMMIT
#define SEALPIR_COMMIT "unknown"
#endif
#ifndef BENCH_CXX_FLAGS
#define BENCH_CXX_FLAGS "unknown"
#endif

using namespace std;
using namespace seal;
using Clock = chrono::steady_clock;

namespace {

double ms_since(Clock::time_point t0) {
  return chrono::duration<double, milli>(Clock::now() - t0).count();
}

uint64_t peak_rss_bytes() {
  struct rusage ru;
  getrusage(RUSAGE_SELF, &ru);
  return static_cast<uint64_t>(ru.ru_maxrss) * 1024ULL; // ru_maxrss is KiB on Linux
}

string json_escape(const string &s) {
  string o;
  for (char c : s) {
    if (c == '"' || c == '\\') {
      o += '\\';
      o += c;
    } else if (static_cast<unsigned char>(c) < 0x20) {
      o += ' ';
    } else {
      o += c;
    }
  }
  return o;
}

// Minimal JSON object builder (flat values plus pre-rendered nested JSON).
class Json {
public:
  Json &s(const string &k, const string &v) { key(k) << '"' << json_escape(v) << '"'; return *this; }
  Json &u(const string &k, uint64_t v) { key(k) << v; return *this; }
  Json &i(const string &k, int64_t v) { key(k) << v; return *this; }
  Json &b(const string &k, bool v) { key(k) << (v ? "true" : "false"); return *this; }
  Json &d(const string &k, double v) {
    if (std::isfinite(v)) {
      key(k) << fixed << setprecision(4) << v;
    } else {
      key(k) << "null";
    }
    return *this;
  }
  Json &raw(const string &k, const string &json) { key(k) << json; return *this; }
  string str() const { return "{" + os_.str() + "}"; }

private:
  ostringstream &key(const string &k) {
    if (!first_) os_ << ',';
    first_ = false;
    os_ << '"' << json_escape(k) << "\":";
    return os_;
  }
  ostringstream os_;
  bool first_ = true;
};

template <typename T> string json_array(const vector<T> &v) {
  ostringstream o;
  o << '[';
  for (size_t k = 0; k < v.size(); k++) o << (k ? "," : "") << v[k];
  o << ']';
  return o.str();
}

string json_array_d(const vector<double> &v) {
  ostringstream o;
  o << '[' << fixed << setprecision(4);
  for (size_t k = 0; k < v.size(); k++) o << (k ? "," : "") << v[k];
  o << ']';
  return o.str();
}

struct Options {
  string mode = "block";  // block | track | probe
  string variant = "api"; // track: api | reuse
  string label = "unnamed";
  string out;
  uint64_t items = 10;
  uint64_t size = 8192;
  uint32_t N = 4096;
  uint32_t logt = 20;
  uint32_t d = 1;
  bool symmetric = true;
  bool batching = true;
  bool mswitch = true;
  int warmup = 1;
  int trials = 10;
  uint64_t seed = 20261121;
  uint64_t track_bytes = 5760000;
  uint64_t chunk = 10240;
  bool probe_db = false;
  int instances = 40;
};

Options parse(int argc, char **argv) {
  Options o;
  for (int k = 1; k < argc; k++) {
    string a = argv[k];
    auto next = [&]() -> string {
      if (k + 1 >= argc) throw invalid_argument("missing value for " + a);
      return argv[++k];
    };
    if (a == "--mode") o.mode = next();
    else if (a == "--variant") o.variant = next();
    else if (a == "--label") o.label = next();
    else if (a == "--out") o.out = next();
    else if (a == "--items") o.items = stoull(next());
    else if (a == "--size") o.size = stoull(next());
    else if (a == "--N") o.N = static_cast<uint32_t>(stoul(next()));
    else if (a == "--logt") o.logt = static_cast<uint32_t>(stoul(next()));
    else if (a == "--d") o.d = static_cast<uint32_t>(stoul(next()));
    else if (a == "--symmetric") o.symmetric = stoi(next()) != 0;
    else if (a == "--batching") o.batching = stoi(next()) != 0;
    else if (a == "--mswitch") o.mswitch = stoi(next()) != 0;
    else if (a == "--warmup") o.warmup = stoi(next());
    else if (a == "--trials") o.trials = stoi(next());
    else if (a == "--seed") o.seed = stoull(next());
    else if (a == "--track-bytes") o.track_bytes = stoull(next());
    else if (a == "--chunk") o.chunk = stoull(next());
    else if (a == "--probe-db") o.probe_db = stoi(next()) != 0;
    else if (a == "--instances") o.instances = stoi(next());
    else throw invalid_argument("unknown option " + a);
  }
  return o;
}

// Deterministic database bytes: std::mt19937_64 output, little-endian, from a fixed seed.
unique_ptr<uint8_t[]> random_bytes(uint64_t n, uint64_t seed) {
  unique_ptr<uint8_t[]> v(new uint8_t[n]);
  mt19937_64 g(seed);
  uint64_t k = 0;
  for (; k + 8 <= n; k += 8) {
    uint64_t x = g();
    memcpy(v.get() + k, &x, 8);
  }
  if (k < n) {
    uint64_t x = g();
    memcpy(v.get() + k, &x, n - k);
  }
  return v;
}

unique_ptr<const uint8_t[]> copy_bytes(const uint8_t *src, uint64_t n) {
  uint8_t *p = new uint8_t[n];
  memcpy(p, src, n);
  return unique_ptr<const uint8_t[]>(p);
}

string build_info_json() {
  Json j;
  j.s("sealpir_commit", SEALPIR_COMMIT)
      .s("seal_version", SEAL_VERSION)
      .s("compiler", string("g++ ") + __VERSION__)
      .s("cxx_flags", BENCH_CXX_FLAGS)
#ifdef NDEBUG
      .b("ndebug", true);
#else
      .b("ndebug", false);
#endif
  return j.str();
}

string config_json(const Options &o, const EncryptionParameters &ep, const PirParams &pp) {
  SEALContext ctx(ep, true);
  auto q = ctx.first_context_data()->qualifiers();
  vector<int> qbits;
  int total_bits = 0;
  for (const auto &m : ep.coeff_modulus()) {
    qbits.push_back(m.bit_count());
    total_bits += m.bit_count();
  }
  const Modulus &t = ep.plain_modulus();
  string sec = q.sec_level == sec_level_type::tc128   ? "tc128"
               : q.sec_level == sec_level_type::tc192 ? "tc192"
               : q.sec_level == sec_level_type::tc256 ? "tc256"
                                                      : "none";
  uint64_t prod = 1;
  for (auto n : pp.nvec) prod *= n;
  Json j;
  j.s("mode", o.mode)
      .s("variant", o.mode == "track" ? o.variant : "api")
      .u("number_of_items", o.items)
      .u("size_per_item", o.mode == "track" ? o.chunk : o.size)
      .u("N", ep.poly_modulus_degree())
      .u("logt", o.logt)
      .u("d", pp.d)
      .b("use_symmetric", pp.enable_symmetric)
      .b("use_batching", pp.enable_batching)
      .b("use_recursive_mod_switching", pp.enable_mswitching)
      .u("plain_modulus", t.value())
      .u("plain_modulus_bits", static_cast<uint64_t>(t.bit_count()))
      .u("usable_bits_per_coeff", static_cast<uint64_t>(floor(log2(t.value()))))
      .b("plain_modulus_is_prime", t.is_prime())
      .b("plain_modulus_eq_1_mod_2N", t.value() % (2ULL * ep.poly_modulus_degree()) == 1)
      .b("context_using_batching", q.using_batching)
      .raw("coeff_modulus_bits", json_array(qbits))
      .i("coeff_modulus_total_bits", total_bits)
      .i("max_bits_tc128_for_N", CoeffModulus::MaxBitCount(ep.poly_modulus_degree(), sec_level_type::tc128))
      .s("seal_sec_level", sec)
      .u("elements_per_plaintext", pp.elements_per_plaintext)
      .u("num_of_plaintexts", pp.num_of_plaintexts)
      .raw("nvec", json_array(pp.nvec))
      .u("plaintexts_after_padding", prod)
      .u("expansion_ratio", pp.expansion_ratio)
      .u("seed", o.seed);
  if (o.mode == "track") {
    j.u("track_bytes", o.track_bytes).u("chunk_bytes", o.chunk).u("blocks", (o.track_bytes + o.chunk - 1) / o.chunk);
  }
  return j.str();
}

// SealPIR's database encoding (PIRServer::set_database + preprocess_database), reproduced
// with SealPIR's public helpers so the "reuse" path multiplies exactly the same plaintexts.
vector<Plaintext> sealpir_encode_ntt(const uint8_t *bytes, uint64_t ele_num, uint64_t ele_size,
                                     const PirParams &pp, const EncryptionParameters &ep,
                                     const SEALContext &ctx, BatchEncoder &enc, Evaluator &ev) {
  const uint32_t logt = static_cast<uint32_t>(floor(log2(ep.plain_modulus().value())));
  const uint32_t N = static_cast<uint32_t>(ep.poly_modulus_degree());
  uint64_t prod = 1;
  for (auto n : pp.nvec) prod *= n;
  const uint64_t ele_per_ptxt = pp.elements_per_plaintext;
  const uint64_t bytes_per_ptxt = ele_per_ptxt * ele_size;
  const uint64_t db_size = ele_num * ele_size;
  const uint64_t coeff_per_ele = coefficients_per_element(logt, ele_size);
  const uint64_t coeff_per_ptxt = ele_per_ptxt * coeff_per_ele;
  if (coeff_per_ptxt > N) throw logic_error("element does not fit in one plaintext");
  vector<Plaintext> out;
  out.reserve(prod);
  uint64_t offset = 0;
  for (uint64_t k = 0; k < pp.num_of_plaintexts && offset < db_size; k++) {
    uint64_t process_bytes = min(bytes_per_ptxt, db_size - offset);
    uint64_t ele_in_chunk = process_bytes / ele_size;
    vector<uint64_t> coefficients(coeff_per_ptxt);
    for (uint64_t e = 0; e < ele_in_chunk; e++) {
      vector<uint64_t> c = bytes_to_coeffs(logt, bytes + offset + ele_size * e, ele_size);
      copy(c.begin(), c.end(), coefficients.begin() + coeff_per_ele * e);
    }
    offset += process_bytes;
    coefficients.resize(N, 1); // SealPIR pads the remaining slots with 1s
    Plaintext plain;
    enc.encode(coefficients, plain);
    out.push_back(move(plain));
  }
  vector<uint64_t> padding(N, 1); // SealPIR's padding plaintexts (coefficient form)
  while (out.size() < prod) {
    Plaintext plain;
    vector_to_plaintext(padding, plain);
    out.push_back(move(plain));
  }
  for (auto &p : out) ev.transform_to_ntt_inplace(p, ctx.first_parms_id());
  return out;
}

// SealPIR's d = 1 inner loop (generate_reply after expansion): products, sums, inverse NTT.
Ciphertext reuse_reply(const vector<Ciphertext> &sel_ntt, const vector<Plaintext> &db, Evaluator &ev) {
  Ciphertext acc, tmp;
  ev.multiply_plain(sel_ntt[0], db[0], acc);
  for (size_t j = 1; j < sel_ntt.size(); j++) {
    ev.multiply_plain(sel_ntt[j], db[j], tmp);
    ev.add_inplace(acc, tmp);
  }
  ev.transform_from_ntt_inplace(acc);
  return acc;
}

// serialize_reply's work for one ciphertext: mod switch to the last prime, then save.
uint64_t modswitch_save(Ciphertext &ct, const SEALContext &ctx, Evaluator &ev, ostream &os) {
  ev.mod_switch_to_inplace(ct, ctx.last_parms_id());
  return static_cast<uint64_t>(ct.save(os));
}

struct Setup {
  EncryptionParameters ep{scheme_type::bfv};
  PirParams pp;
  double t_params = 0;
};

Setup make_params(const Options &o, uint64_t ele_size) {
  Setup s;
  auto t0 = Clock::now();
  gen_encryption_params(o.N, o.logt, s.ep);
  verify_encryption_params(s.ep);
  gen_pir_params(o.items, ele_size, o.d, s.ep, s.pp, o.symmetric, o.batching, o.mswitch);
  s.t_params = ms_since(t0);
  return s;
}

void run_block_trial(const Options &o, const uint8_t *db, uint64_t idx, int trial, bool warm,
                     ofstream &out) {
  Setup st = make_params(o, o.size);
  const EncryptionParameters &ep = st.ep;
  const PirParams &pp = st.pp;
  Json tm, sz;
  tm.d("params", st.t_params);

  auto t0 = Clock::now();
  PIRClient client(ep, pp);
  tm.d("client_keygen", ms_since(t0));

  t0 = Clock::now();
  GaloisKeys gk = client.generate_galois_keys();
  tm.d("galois_keygen", ms_since(t0));

  stringstream gks;
  t0 = Clock::now();
  uint64_t gk_bytes = static_cast<uint64_t>(gk.save(gks));
  tm.d("galois_serialize", ms_since(t0));

  t0 = Clock::now();
  PIRServer server(ep, pp);
  tm.d("server_init", ms_since(t0));

  SEALContext sctx(ep, true); // PIRServer's context is private; same parameters -> same parms_id
  t0 = Clock::now();
  GaloisKeys gk_srv;
  gk_srv.load(sctx, gks);
  server.set_galois_key(0, gk_srv);
  tm.d("galois_deserialize_set", ms_since(t0));

  auto bytes = copy_bytes(db, o.items * o.size);
  t0 = Clock::now();
  server.set_database(bytes, o.items, o.size);
  double t_set = ms_since(t0);
  t0 = Clock::now();
  server.preprocess_database();
  double t_pre = ms_since(t0);
  tm.d("db_set_database", t_set).d("db_preprocess_ntt", t_pre).d("db_setup_total", t_set + t_pre);

  const uint64_t fv_index = client.get_fv_index(idx);
  const uint64_t fv_offset = client.get_fv_offset(idx);

  stringstream qs;
  t0 = Clock::now();
  int q_bytes = client.generate_serialized_query(fv_index, qs);
  tm.d("query_gen_serialized", ms_since(t0));

  t0 = Clock::now();
  PirQuery q = server.deserialize_query(qs);
  tm.d("query_deserialize", ms_since(t0));

  t0 = Clock::now();
  PirReply reply = server.generate_reply(q, 0);
  double t_reply = ms_since(t0);

  stringstream rs;
  t0 = Clock::now();
  int r_bytes = server.serialize_reply(reply, rs); // includes mod switch to the last prime
  double t_rser = ms_since(t0);
  tm.d("reply_gen", t_reply).d("reply_modswitch_serialize", t_rser).d("server_reply_total", t_reply + t_rser);

  SEALContext cctx(ep, true);
  t0 = Clock::now();
  PirReply reply_c(reply.size());
  for (auto &c : reply_c) c.load(cctx, rs);
  tm.d("reply_deserialize", ms_since(t0));

  t0 = Clock::now();
  vector<uint8_t> elem = client.decode_reply(reply_c, fv_offset);
  tm.d("client_decode", ms_since(t0));

  bool correct = elem.size() == o.size && memcmp(elem.data(), db + idx * o.size, o.size) == 0;

  // Expansion alone (SealPIR public API), one query ciphertext per dimension as in generate_reply.
  double t_expand = 0;
  bool expand_measured = true;
  for (uint32_t k = 0; k < pp.d; k++) {
    if (pp.nvec[k] > ep.poly_modulus_degree() || q[k].size() != 1) {
      expand_measured = false;
      break;
    }
    t0 = Clock::now();
    vector<Ciphertext> e = server.expand_query(q[k][0], static_cast<uint32_t>(pp.nvec[k]), 0);
    t_expand += ms_since(t0);
  }
  tm.d("expand_only", expand_measured ? t_expand : NAN);

  // d = 1: SealPIR's inner loop with an already expanded NTT query (query reuse across blocks).
  bool reuse_correct = false;
  uint64_t reuse_bytes = 0;
  if (pp.d == 1 && expand_measured) {
    Evaluator ev(sctx);
    BatchEncoder enc(sctx);
    vector<Plaintext> pts = sealpir_encode_ntt(db, o.items, o.size, pp, ep, sctx, enc, ev);
    t0 = Clock::now();
    vector<Ciphertext> sel = server.expand_query(q[0][0], static_cast<uint32_t>(pp.nvec[0]), 0);
    for (auto &c : sel) ev.transform_to_ntt_inplace(c);
    tm.d("reuse_expand_and_ntt_once", ms_since(t0));
    t0 = Clock::now();
    Ciphertext acc = reuse_reply(sel, pts, ev);
    double t_rr = ms_since(t0);
    stringstream rs2;
    t0 = Clock::now();
    reuse_bytes = modswitch_save(acc, sctx, ev, rs2);
    double t_rs = ms_since(t0);
    tm.d("reuse_reply_gen", t_rr).d("reuse_modswitch_serialize", t_rs).d("reuse_server_block_total", t_rr + t_rs);
    PirReply r2{acc};
    vector<uint8_t> e2 = client.decode_reply(r2, fv_offset);
    reuse_correct = e2.size() == o.size && memcmp(e2.data(), db + idx * o.size, o.size) == 0;
  }

  const uint64_t galois_elements = static_cast<uint64_t>(count_if(
      gk.data().begin(), gk.data().end(), [](const vector<PublicKey> &v) { return !v.empty(); }));
  sz.u("query", static_cast<uint64_t>(q_bytes))
      .u("query_ciphertexts", [&] { uint64_t n = 0; for (auto &v : q) n += v.size(); return n; }())
      .u("galois_keys", gk_bytes)
      .u("galois_elements", galois_elements)
      .u("reply", static_cast<uint64_t>(r_bytes))
      .u("reply_ciphertexts", reply.size())
      .u("payload_bytes", o.size);
  if (pp.d == 1) sz.u("reuse_reply", reuse_bytes);

  Json j;
  j.s("label", o.label).i("trial", trial).b("warmup", warm)
      .raw("config", config_json(o, ep, pp))
      .u("selected_index", idx)
      .raw("timings_ms", tm.str())
      .raw("sizes_bytes", sz.str())
      .b("correct", correct);
  if (pp.d == 1) j.b("reuse_correct", reuse_correct);
  j.u("peak_rss_bytes", peak_rss_bytes()).raw("build", build_info_json());
  out << j.str() << "\n";
  out.flush();
  cerr << o.label << " trial " << trial << (warm ? " (warm-up)" : "") << ": correct=" << correct
       << " reply_gen_ms=" << t_reply << " reply_bytes=" << r_bytes << endl;
}

void run_track_trial(const Options &o, const uint8_t *tracks, uint64_t idx, int trial, bool warm,
                     ofstream &out) {
  const uint64_t R = o.items, C = o.chunk, T = o.track_bytes;
  const uint64_t nblocks = (T + C - 1) / C;
  Setup st = make_params(o, C);
  const EncryptionParameters &ep = st.ep;
  const PirParams &pp = st.pp;
  if (pp.elements_per_plaintext != 1) throw logic_error("track mode expects one chunk per plaintext");
  Json tm, sz;
  tm.d("params", st.t_params);

  auto t0 = Clock::now();
  PIRClient client(ep, pp);
  tm.d("client_keygen", ms_since(t0));
  t0 = Clock::now();
  GaloisKeys gk = client.generate_galois_keys();
  tm.d("galois_keygen", ms_since(t0));
  stringstream gks;
  uint64_t gk_bytes = static_cast<uint64_t>(gk.save(gks));
  SEALContext sctx(ep, true);
  SEALContext cctx(ep, true);
  GaloisKeys gk_srv;
  gk_srv.load(sctx, gks);

  // One query for the whole track, reused for every block database.
  const uint64_t fv_index = client.get_fv_index(idx), fv_offset = client.get_fv_offset(idx);
  stringstream qs;
  t0 = Clock::now();
  int q_bytes = client.generate_serialized_query(fv_index, qs);
  tm.d("query_gen_serialized", ms_since(t0));

  unique_ptr<uint8_t[]> recovered(new uint8_t[nblocks * C]);
  unique_ptr<uint8_t[]> block(new uint8_t[R * C]);
  auto fill_block = [&](uint64_t j) {
    for (uint64_t r = 0; r < R; r++) {
      uint64_t start = j * C, n = (start < T) ? min(C, T - start) : 0;
      memset(block.get() + r * C, 0, C); // the last chunk is zero-padded
      if (n) memcpy(block.get() + r * C, tracks + r * T + start, n);
    }
  };

  double s_init = 0, s_gal = 0, s_set = 0, s_pre = 0, s_reply = 0, s_rser = 0, s_rdes = 0, s_dec = 0;
  double t_qdes = 0, t_expand_once = 0, t_db_build = 0;
  uint64_t reply_bytes = 0, reply_cts = 0;
  vector<double> per_block_reply;
  per_block_reply.reserve(nblocks);

  auto client_side = [&](stringstream &rs, uint64_t ncts, uint64_t j) {
    auto t1 = Clock::now();
    PirReply rc(ncts);
    for (auto &c : rc) c.load(cctx, rs);
    s_rdes += ms_since(t1);
    t1 = Clock::now();
    vector<uint8_t> e = client.decode_reply(rc, fv_offset);
    s_dec += ms_since(t1);
    if (e.size() != C) throw logic_error("decoded chunk has wrong size");
    memcpy(recovered.get() + j * C, e.data(), C);
  };

  if (o.variant == "api") {
    PirQuery q;
    for (uint64_t j = 0; j < nblocks; j++) {
      fill_block(j);
      auto bytes = copy_bytes(block.get(), R * C);
      t0 = Clock::now();
      PIRServer server(ep, pp);
      s_init += ms_since(t0);
      t0 = Clock::now();
      server.set_galois_key(0, gk_srv);
      s_gal += ms_since(t0);
      t0 = Clock::now();
      server.set_database(bytes, R, C);
      s_set += ms_since(t0);
      t0 = Clock::now();
      server.preprocess_database();
      s_pre += ms_since(t0);
      if (j == 0) {
        t0 = Clock::now();
        q = server.deserialize_query(qs);
        t_qdes = ms_since(t0);
      }
      t0 = Clock::now();
      PirReply reply = server.generate_reply(q, 0);
      double tr = ms_since(t0);
      stringstream rs;
      t0 = Clock::now();
      int rb = server.serialize_reply(reply, rs);
      double ts = ms_since(t0);
      s_reply += tr;
      s_rser += ts;
      per_block_reply.push_back(tr + ts);
      reply_bytes += static_cast<uint64_t>(rb);
      reply_cts += reply.size();
      client_side(rs, reply.size(), j);
    }
    tm.d("server_init_sum", s_init).d("galois_set_sum", s_gal).d("db_set_database_sum", s_set)
        .d("db_preprocess_ntt_sum", s_pre);
  } else if (o.variant == "reuse") {
    Evaluator ev(sctx);
    BatchEncoder enc(sctx);
    vector<vector<Plaintext>> dbs(nblocks);
    t0 = Clock::now();
    for (uint64_t j = 0; j < nblocks; j++) {
      fill_block(j);
      dbs[j] = sealpir_encode_ntt(block.get(), R, C, pp, ep, sctx, enc, ev);
    }
    t_db_build = ms_since(t0);
    PIRServer server(ep, pp); // used only for deserialize_query and expand_query
    server.set_galois_key(0, gk_srv);
    t0 = Clock::now();
    PirQuery q = server.deserialize_query(qs);
    t_qdes = ms_since(t0);
    t0 = Clock::now();
    vector<Ciphertext> sel = server.expand_query(q[0][0], static_cast<uint32_t>(pp.nvec[0]), 0);
    for (auto &c : sel) ev.transform_to_ntt_inplace(c);
    t_expand_once = ms_since(t0);
    for (uint64_t j = 0; j < nblocks; j++) {
      t0 = Clock::now();
      Ciphertext acc = reuse_reply(sel, dbs[j], ev);
      double tr = ms_since(t0);
      stringstream rs;
      t0 = Clock::now();
      uint64_t rb = modswitch_save(acc, sctx, ev, rs);
      double ts = ms_since(t0);
      s_reply += tr;
      s_rser += ts;
      per_block_reply.push_back(tr + ts);
      reply_bytes += rb;
      reply_cts += 1;
      client_side(rs, 1, j);
    }
    tm.d("db_encode_ntt_all_blocks", t_db_build).d("expand_and_ntt_once", t_expand_once);
  } else {
    throw invalid_argument("unknown --variant " + o.variant);
  }

  bool correct = memcmp(recovered.get(), tracks + idx * T, T) == 0;
  vector<double> sorted = per_block_reply;
  sort(sorted.begin(), sorted.end());
  double med = sorted.empty() ? NAN : (sorted.size() % 2 ? sorted[sorted.size() / 2]
                                                          : 0.5 * (sorted[sorted.size() / 2 - 1] + sorted[sorted.size() / 2]));
  double server_query_total = t_qdes + t_expand_once + s_reply + s_rser;
  tm.d("query_deserialize", t_qdes)
      .d("reply_gen_sum", s_reply)
      .d("reply_modswitch_serialize_sum", s_rser)
      .d("server_query_total", server_query_total)
      .d("reply_deserialize_sum", s_rdes)
      .d("client_decode_sum", s_dec)
      .d("per_block_server_median", med)
      .d("per_block_server_min", sorted.empty() ? NAN : sorted.front())
      .d("per_block_server_max", sorted.empty() ? NAN : sorted.back());
  sz.u("query", static_cast<uint64_t>(q_bytes))
      .u("galois_keys", gk_bytes)
      .u("reply_total", reply_bytes)
      .u("reply_ciphertexts", reply_cts)
      .u("payload_bytes", T)
      .u("blocks", nblocks);

  Json j;
  j.s("label", o.label).i("trial", trial).b("warmup", warm)
      .raw("config", config_json(o, ep, pp))
      .u("selected_index", idx)
      .raw("timings_ms", tm.str())
      .raw("sizes_bytes", sz.str())
      .b("correct", correct)
      .u("peak_rss_bytes", peak_rss_bytes())
      .raw("build", build_info_json());
  out << j.str() << "\n";
  out.flush();
  cerr << o.label << " trial " << trial << (warm ? " (warm-up)" : "") << ": correct=" << correct
       << " server_query_total_ms=" << server_query_total << " reply_total_bytes=" << reply_bytes << endl;
}

uint64_t current_rss_bytes() {
  ifstream f("/proc/self/statm");
  uint64_t size = 0, resident = 0;
  f >> size >> resident;
  return resident * static_cast<uint64_t>(sysconf(_SC_PAGESIZE));
}

int run_instances(const Options &o, ofstream &out) {
  Setup st = make_params(o, o.size);
  PIRClient client(st.ep, st.pp);
  GaloisKeys gk = client.generate_galois_keys();
  stringstream gks;
  uint64_t gk_bytes = static_cast<uint64_t>(gk.save(gks));
  SEALContext sctx(st.ep, true);
  GaloisKeys gk_srv;
  gk_srv.load(sctx, gks);
  // In-memory size of the Galois keys: sum over key-switching ciphertexts of their data size.
  uint64_t gk_mem = 0;
  for (const auto &v : gk_srv.data())
    for (const auto &pk : v) gk_mem += pk.data().dyn_array().size() * sizeof(uint64_t);
  auto db = random_bytes(o.items * o.size, o.seed);
  vector<unique_ptr<PIRServer>> servers;
  vector<uint64_t> rss;
  rss.push_back(current_rss_bytes());
  double t_total = 0;
  for (int k = 0; k < o.instances; k++) {
    auto bytes = copy_bytes(db.get(), o.items * o.size);
    auto t0 = Clock::now();
    servers.emplace_back(new PIRServer(st.ep, st.pp));
    servers.back()->set_galois_key(0, gk_srv);
    servers.back()->set_database(bytes, o.items, o.size);
    servers.back()->preprocess_database();
    t_total += ms_since(t0);
    rss.push_back(current_rss_bytes());
  }
  double per_instance = rss.size() > 1 ? double(rss.back() - rss.front()) / (rss.size() - 1) : NAN;
  Json j;
  j.s("label", o.label).s("mode", "instances")
      .raw("config", config_json(o, st.ep, st.pp))
      .i("instances", o.instances)
      .u("galois_keys_serialized_bytes", gk_bytes)
      .u("galois_keys_in_memory_bytes", gk_mem)
      .u("db_ntt_plaintext_bytes_per_instance", [&] {
        uint64_t prod = 1; for (auto n : st.pp.nvec) prod *= n;
        return prod * st.ep.poly_modulus_degree() * (st.ep.coeff_modulus().size() - 1) * sizeof(uint64_t); }())
      .d("setup_ms_per_instance", t_total / max(1, o.instances))
      .raw("rss_bytes_after_k_instances", json_array(rss))
      .d("rss_bytes_per_instance_mean", per_instance)
      .u("peak_rss_bytes", peak_rss_bytes())
      .raw("build", build_info_json());
  out << j.str() << "\n";
  return 0;
}

int run_probe(const Options &o, ofstream &out) {
  EncryptionParameters ep(scheme_type::bfv);
  gen_encryption_params(o.N, o.logt, ep);
  verify_encryption_params(ep);
  cerr << "probe: gen_pir_params(items=" << o.items << ", size=" << o.size << ", d=" << o.d
       << ", batching=" << o.batching << ")" << endl;
  PirParams pp;
  gen_pir_params(o.items, o.size, o.d, ep, pp, o.symmetric, o.batching, o.mswitch);
  cerr << "probe: gen_pir_params returned; elements_per_plaintext=" << pp.elements_per_plaintext
       << " num_of_plaintexts=" << pp.num_of_plaintexts << endl;
  bool db_ok = false;
  if (o.probe_db) {
    PIRServer server(ep, pp);
    auto bytes = random_bytes(o.items * o.size, o.seed);
    unique_ptr<const uint8_t[]> cbytes(bytes.release());
    cerr << "probe: PIRServer::set_database(...)" << endl;
    server.set_database(cbytes, o.items, o.size);
    db_ok = true;
  }
  Json j;
  j.s("label", o.label).s("mode", "probe")
      .u("number_of_items", o.items).u("size_per_item", o.size).u("d", o.d)
      .b("use_batching", o.batching)
      .u("elements_per_plaintext", pp.elements_per_plaintext)
      .u("num_of_plaintexts", pp.num_of_plaintexts)
      .raw("nvec", json_array(pp.nvec))
      .b("set_database_called", o.probe_db).b("set_database_returned", db_ok)
      .raw("build", build_info_json());
  out << j.str() << "\n";
  return 0;
}

} // namespace

int main(int argc, char **argv) {
  Options o;
  try {
    o = parse(argc, argv);
  } catch (const exception &e) {
    cerr << "argument error: " << e.what() << endl;
    return 2;
  }
  if (o.out.empty()) {
    cerr << "--out is required" << endl;
    return 2;
  }
  ofstream out(o.out, ios::app);
  if (!out) {
    cerr << "cannot open " << o.out << endl;
    return 2;
  }
  // SealPIR prints progress to std::cout; discard it. The guard restores cout's buffer before
  // devnull is destroyed (otherwise the final flush of cout at exit touches a dead buffer).
  ofstream devnull("/dev/null");
  struct CoutGuard {
    streambuf *old;
    ~CoutGuard() { cout.rdbuf(old); }
  } guard{cout.rdbuf(devnull.rdbuf())};

  try {
    if (o.mode == "probe") return run_probe(o, out);
    if (o.mode == "instances") return run_instances(o, out);
    mt19937_64 pick(o.seed ^ 0x9e3779b97f4a7c15ULL);
    if (o.mode == "block") {
      auto db = random_bytes(o.items * o.size, o.seed);
      for (int k = 0; k < o.warmup + o.trials; k++) {
        uint64_t idx = pick() % o.items;
        run_block_trial(o, db.get(), idx, k - o.warmup, k < o.warmup, out);
      }
    } else if (o.mode == "track") {
      auto tracks = random_bytes(o.items * o.track_bytes, o.seed);
      for (int k = 0; k < o.warmup + o.trials; k++) {
        uint64_t idx = pick() % o.items;
        run_track_trial(o, tracks.get(), idx, k - o.warmup, k < o.warmup, out);
      }
    } else {
      cerr << "unknown --mode " << o.mode << endl;
      return 2;
    }
  } catch (const exception &e) {
    cerr << "error: " << e.what() << endl;
    Json j;
    j.s("label", o.label).s("error", e.what()).u("peak_rss_bytes", peak_rss_bytes());
    out << j.str() << "\n";
    return 1;
  }
  return 0;
}
