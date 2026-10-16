"""Geração das chaves, consulta privada e recuperação da resposta."""

import tenseal as ts


# N criptográfico = quantidade de slots, não é o número de músicas.
POLY_MODULUS_DEGREE = 4096
PLAIN_MODULUS = 65537  # Primo = 1 mod (2 * 4096); permite batching BFV.


class PIRClient:
    def __init__(self, record_count: int, record_size: int):
        if record_count < 1:
            raise ValueError("O catálogo precisa ter pelo menos um registro.")
        if not 1 <= record_size <= POLY_MODULUS_DEGREE:
            raise ValueError("Cada registro precisa ter entre 1 e 4096 bytes.")
        self.record_count = record_count
        self.record_size = record_size
        self._context = ts.context(
            ts.SCHEME_TYPE.BFV,
            poly_modulus_degree=POLY_MODULUS_DEGREE,
            plain_modulus=PLAIN_MODULUS,
            n_threads=1,
        )

    def public_context(self) -> bytes:
        """Exporta os parâmetros e a chave pública; nunca a chave secreta."""
        return self._context.serialize(
            save_public_key=True,
            save_secret_key=False,
            save_galois_keys=False,
            save_relin_keys=False,
        )

    def create_query(self, index: int) -> tuple[bytes, ...]:
        """Um criptograma por registro, com o seletor repetido nos slots.

        Para escolher o registro 2 entre 4 registros, os seletores lógicos são
        [0, 0, 1, 0]. Cada seletor é cifrado com aleatoriedade nova. Repeti-lo
        nos slots permite selecionar todos os bytes do bloco em paralelo.
        O índice nunca faz parte do valor retornado.
        """
        if not 0 <= index < self.record_count:
            raise ValueError("Índice fora do catálogo.")
        return tuple(
            ts.bfv_vector(self._context, [int(i == index)] * self.record_size)
            .serialize()
            for i in range(self.record_count)
        )

    def recover(self, response: bytes) -> bytes:
        values = ts.bfv_vector_from(self._context, response).decrypt()
        if len(values) != self.record_size or any(not 0 <= x <= 255 for x in values):
            raise ValueError("Resposta não corresponde a um bloco de bytes válido.")
        return bytes(values)
