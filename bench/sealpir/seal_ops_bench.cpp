// Calibration micro-benchmark: cost of the SEAL primitives used by SealPIR and by our design,
// measured with the project-local SEAL 4.0.0 build (GCC 7.3.1). seal_ops_bench.py runs the same
// operations through TenSEAL 0.3.14's sealapi (SEAL 4.1.1). Same parameters, same machine,
// single thread. Prints one JSON object per plain modulus.
//   usage: seal_ops_bench [reps] [seed]
#include <seal/seal.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <map>
#include <random>
#include <sstream>
#include <string>
#include <vector>

using namespace std;
using namespace seal;
using Clock = chrono::steady_clock;

namespace {

struct Stat {
  double median, p25, p75, min;
};

Stat stats(vector<double> v) {
  sort(v.begin(), v.end());
  auto q = [&](double f) {
    double pos = f * (v.size() - 1);
    size_t lo = static_cast<size_t>(pos);
    size_t hi = min(lo + 1, v.size() - 1);
    return v[lo] + (pos - lo) * (v[hi] - v[lo]);
  };
  return {q(0.5), q(0.25), q(0.75), v.front()};
}

template <typename F> Stat time_op(int reps, int warm, F f) {
  for (int k = 0; k < warm; k++) f();
  vector<double> ms;
  ms.reserve(reps);
  for (int k = 0; k < reps; k++) {
    auto t0 = Clock::now();
    f();
    ms.push_back(chrono::duration<double, milli>(Clock::now() - t0).count());
  }
  return stats(ms);
}

void run(uint64_t t_value, int reps, uint64_t seed) {
  const size_t N = 4096;
  const int R = 10;
  EncryptionParameters parms(scheme_type::bfv);
  parms.set_poly_modulus_degree(N);
  parms.set_coeff_modulus(CoeffModulus::BFVDefault(N));
  parms.set_plain_modulus(Modulus(t_value));
  SEALContext ctx(parms, true, sec_level_type::tc128);
  KeyGenerator kg(ctx);
  PublicKey pk;
  kg.create_public_key(pk);
  GaloisKeys gk;
  kg.create_galois_keys(vector<uint32_t>{3}, gk);
  Encryptor encryptor(ctx, pk);
  Evaluator ev(ctx);
  BatchEncoder be(ctx);

  mt19937_64 g(seed);
  auto rand_pt = [&](bool ntt) {
    vector<uint64_t> v(N);
    for (auto &x : v) x = g() % t_value;
    Plaintext p;
    be.encode(v, p);
    if (ntt) ev.transform_to_ntt_inplace(p, ctx.first_parms_id());
    return p;
  };
  Plaintext pt = rand_pt(false);
  Ciphertext ct;
  encryptor.encrypt(pt, ct);
  Ciphertext ct_ntt;
  ev.transform_to_ntt(ct, ct_ntt);
  vector<Plaintext> pts;
  vector<Ciphertext> sel(R);
  for (int i = 0; i < R; i++) {
    pts.push_back(rand_pt(true));
    Plaintext s = rand_pt(false);
    encryptor.encrypt(s, sel[i]);
    ev.transform_to_ntt_inplace(sel[i]);
  }
  Plaintext pt_ntt = pts[0];

  map<string, Stat> res;
  Ciphertext dest, acc, tmp;
  res["ntt_fwd"] = time_op(reps, 20, [&] { ev.transform_to_ntt(ct, dest); });
  res["ntt_inv"] = time_op(reps, 20, [&] { ev.transform_from_ntt(ct_ntt, dest); });
  res["mul_plain_ntt"] = time_op(reps, 20, [&] { ev.multiply_plain(ct_ntt, pt_ntt, dest); });
  acc = ct_ntt;
  res["add_inplace"] = time_op(reps, 20, [&] { ev.add_inplace(acc, ct_ntt); });
  res["mod_switch_next"] = time_op(reps, 20, [&] { ev.mod_switch_to_next(ct, dest); });
  res["apply_galois"] = time_op(reps, 20, [&] { ev.apply_galois(ct, 3, gk, dest); });
  res["block_R10_products"] = time_op(reps, 20, [&] {
    ev.multiply_plain(sel[0], pts[0], acc);
    for (int i = 1; i < R; i++) {
      ev.multiply_plain(sel[i], pts[i], tmp);
      ev.add_inplace(acc, tmp);
    }
  });
  res["block_R10_full"] = time_op(reps, 20, [&] {
    ev.multiply_plain(sel[0], pts[0], acc);
    for (int i = 1; i < R; i++) {
      ev.multiply_plain(sel[i], pts[i], tmp);
      ev.add_inplace(acc, tmp);
    }
    ev.transform_from_ntt_inplace(acc);
    ev.mod_switch_to_next_inplace(acc);
  });
  Ciphertext last;
  ev.mod_switch_to_next(ct, last);
  res["save_last_level_zstd"] = time_op(reps, 20, [&] {
    stringstream ss;
    last.save(ss);
  });

  ostringstream o;
  o << fixed << setprecision(5);
  o << "{\"impl\":\"cpp\",\"seal_version\":\"" << SEAL_VERSION << "\",\"compiler\":\"g++ " << __VERSION__
    << "\",\"N\":" << N << ",\"plain_modulus\":" << t_value << ",\"reps\":" << reps << ",\"ops_ms\":{";
  bool first = true;
  for (auto &kv : res) {
    o << (first ? "" : ",") << "\"" << kv.first << "\":{\"median\":" << kv.second.median
      << ",\"p25\":" << kv.second.p25 << ",\"p75\":" << kv.second.p75 << ",\"min\":" << kv.second.min << "}";
    first = false;
  }
  o << "}}";
  cout << o.str() << endl;
}

} // namespace

int main(int argc, char **argv) {
  int reps = argc > 1 ? stoi(argv[1]) : 200;
  uint64_t seed = argc > 2 ? stoull(argv[2]) : 20261121;
  run(65537, reps, seed);                                   // our design's t
  run(PlainModulus::Batching(4096, 21).value(), reps, seed); // SealPIR's t for logt = 20
  return 0;
}
