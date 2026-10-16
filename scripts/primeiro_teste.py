"""Executa PIR real sobre um bloco PCM de cada WAV em data/samples."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
from time import perf_counter
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=ROOT / "data" / "samples")
    parser.add_argument("--block-size", type=int, default=1024,
                        help="Bytes PCM por registro, de 1 a 4096 (padrão: 1024).")
    parser.add_argument("--block-index", type=int, default=0,
                        help="Posição pública do bloco em todos os arquivos (padrão: 0).")
    parser.add_argument("--select", type=int, default=None,
                        help="Índice da faixa, começando em 0. Omitir testa todas.")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    if args.output_dir is None:
        args.output_dir = ROOT / "results" / f"blocos_{args.block_size}"

    return args


def main() -> int:
    args = arguments()
    try:
        import tenseal as ts
        from pir_demo.audio import load_records
        from pir_demo.client import PIRClient, POLY_MODULUS_DEGREE, PLAIN_MODULUS
        from pir_demo.server import PIRServer
    except ImportError as error:
        print(f"Dependência ausente: {error}.\nExecute: python -m pip install -r requirements-pir.txt",
              file=sys.stderr)
        return 2

    try:
        audio_records = load_records(args.samples, args.block_size, args.block_index)
        records = [item.block for item in audio_records]
        count = len(records)
        if args.select is not None and not 0 <= args.select < count:
            raise ValueError(f"--select precisa estar entre 0 e {count - 1}.")

        print(f"Catálogo local: {count} arquivos; índices em ordem alfabética.")
        for index, item in enumerate(audio_records):
            print(f"  [{index}] {item.filename} | {item.payload_bytes} bytes PCM")
        print(f"Bloco público: {args.block_index} | tamanho: {args.block_size} bytes")
        if len(set(records)) < count:
            print("AVISO: há blocos iguais; recuperar bytes iguais não distingue esses arquivos.")

        started = perf_counter()
        client = PIRClient(count, args.block_size)
        public_context = client.public_context()
        client_setup_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        server = PIRServer(records, public_context)
        server_setup_ms = (perf_counter() - started) * 1000
        print(f"Chave secreta presente no servidor: {server.has_secret_key}")

        indices = range(count) if args.select is None else [args.select]
        args.output_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for index in indices:
            started = perf_counter()
            query = client.create_query(index)
            query_ms = (perf_counter() - started) * 1000

            # A fronteira cliente/servidor transporta APENAS a consulta cifrada.
            # O índice abaixo permanece no executor de testes (lado cliente).
            started = perf_counter()
            response = server.answer(query)
            server_ms = (perf_counter() - started) * 1000

            started = perf_counter()
            recovered = client.recover(response)
            recovery_ms = (perf_counter() - started) * 1000
            expected = records[index]
            matches = recovered == expected
            digest = hashlib.sha256(recovered).hexdigest()
            item = audio_records[index]
            output_name = f"{index:03d}_{Path(item.filename).stem}_bloco_{args.block_index:04d}.bin"
            (args.output_dir / output_name).write_bytes(recovered)
            row = {
                "index": index,
                "filename": item.filename,
                "valid_bytes": item.valid_bytes,
                "padding_bytes": args.block_size - item.valid_bytes,
                "matches": matches,
                "sha256_expected": hashlib.sha256(expected).hexdigest(),
                "sha256_recovered": digest,
                "query_ms": query_ms,
                "server_ms": server_ms,
                "recovery_ms": recovery_ms,
                "total_local_ms": query_ms + server_ms + recovery_ms,
                "query_payload_bytes": sum(len(part) for part in query),
                "response_payload_bytes": len(response),
                "output": output_name,
            }
            rows.append(row)
            print(f"{'OK' if matches else 'FALHA'} [{index}] {item.filename} | "
                  f"consulta {query_ms:.1f} ms | servidor {server_ms:.1f} ms | "
                  f"recuperação {recovery_ms:.1f} ms | "
                  f"envio {row['query_payload_bytes']} B | resposta {len(response)} B")

        report = {
            "python_version": platform.python_version(),
            "tenseal_version": ts.__version__,
            "scheme": "BFV",
            "poly_modulus_degree": POLY_MODULUS_DEGREE,
            "plain_modulus": PLAIN_MODULUS,
            "record_count": count,
            "block_size": args.block_size,
            "block_index": args.block_index,
            "client_setup_ms": client_setup_ms,
            "server_setup_ms": server_setup_ms,
            "public_context_bytes": len(public_context),
            "server_has_secret_key": server.has_secret_key,
            "all_correct": all(row["matches"] for row in rows),
            "scope": "Teste local em um processo; tempos sem rede; bytes sem framing HTTP/TCP.",
            "runs": rows,
        }
        report_path = args.output_dir / "resultado.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nResultado: {sum(row['matches'] for row in rows)}/{len(rows)} recuperações corretas.")
        print(f"Relatório: {report_path}")
        print("Os arquivos .bin contêm um bloco PCM, não uma música completa reproduzível.")
        return 0 if report["all_correct"] else 1
    except (ValueError, OSError, EOFError, wave.Error) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
