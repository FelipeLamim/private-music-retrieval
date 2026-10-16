"""Avaliação homomórfica: o servidor não recebe índice nem chave secreta."""

import tenseal as ts


class PIRServer:
    def __init__(self, records: list[bytes], public_context: bytes):
        if not records or not records[0]:
            raise ValueError("A base precisa conter registros não vazios.")
        self.record_size = len(records[0])
        if self.record_size > 4096:
            raise ValueError("O teste admite no máximo 4096 bytes por registro.")
        if any(len(record) != self.record_size for record in records):
            raise ValueError("Todos os registros precisam ter o mesmo tamanho.")
        self._records = [list(record) for record in records]
        self._context = ts.context_from(public_context, n_threads=1)
        if self._context.has_secret_key():
            raise ValueError("O servidor recebeu uma chave secreta indevidamente.")
        if not self._context.has_public_key():
            raise ValueError("O servidor precisa do contexto com a chave pública.")

    @property
    def has_secret_key(self) -> bool:
        return self._context.has_secret_key()

    def answer(self, query: tuple[bytes, ...]) -> bytes:
        if len(query) != len(self._records):
            raise ValueError("Quantidade de seletores diferente do tamanho da base.")

        # A cifra de zero usa a chave pública. Também permite responder quando
        # a base inteira contém zeros, sem produzir um criptograma transparente.
        result = ts.bfv_vector(self._context, [0] * self.record_size)
        for serialized_selector, record in zip(query, self._records):
            selector = ts.bfv_vector_from(self._context, serialized_selector)
            if selector.size() != self.record_size:
                raise ValueError("Seletor com tamanho incompatível com o bloco.")
            if any(record):
                result += selector * record
            # A decisão acima depende apenas de dados públicos do banco.
            # Multiplicação por plaintext totalmente zero é evitada porque
            # pode gerar um criptograma transparente na biblioteca SEAL.
        return result.serialize()
