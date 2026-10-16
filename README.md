# Primeiro teste: PIR de um servidor com BFV

Este pacote implementa o esquema didático de seleção por vetor one-hot cifrado.
Usa Python, TenSEAL e criptografia homomórfica BFV real (não simula a cifragem).
TenSEAL usa Microsoft SEAL internamente. Não é uma implementação do SealPIR.

## O que este primeiro teste faz

1. Lista os WAVs em `data/samples` em ordem alfabética e atribui índices de 0 em diante.
2. Extrai um bloco de áudio PCM de cada arquivo, sem o cabeçalho WAV: 1024 bytes por padrão ou o tamanho indicado por `--block-size`.
3. Trata esses seis blocos como seis registros pequenos do banco PIR.
4. Gera no cliente uma consulta cifrada para selecionar um dos registros.
5. O servidor calcula a resposta usando o banco em claro e somente o contexto público.
6. O cliente decifra e compara os bytes recuperados com o bloco esperado.
7. Por padrão, repete para todos os índices e grava resultados e tempos.

Foram testados blocos de **1024 e 4096 bytes**, com seis recuperações corretas em cada configuração.
Cada registro desses tamanhos cabe em um único plaintext BFV usando batching.
Cada resposta contém um criptograma BFV, serializado pelo TenSEAL. A consulta
didática contém um criptograma por registro: seis para seis áudios.

O arquivo WAV completo tem aproximadamente 160 KB e NÃO cabe neste único registro.
Por isso, este experimento recupera apenas um bloco de cada áudio. A recuperação
de uma faixa inteira, sua reprodução e a rede entram em etapas posteriores.

## Organização do projeto

Os principais arquivos são:

- `requirements-pir.txt`: dependências com versões fixadas.
- `src/pir_demo/client.py`: chaves, consulta e decifragem.
- `src/pir_demo/server.py`: processamento homomórfico no servidor.
- `src/pir_demo/audio.py`: leitura e preenchimento dos blocos.
- `scripts/primeiro_teste.py`: executor do experimento e geração do relatório.
- `tests/test_pir.py`: testes automatizados.

Os seis WAVs sintéticos estão em `data/samples`, na mesma raiz que
`src`, `scripts` e `requirements-pir.txt`.

## Instalação com Conda (Ubuntu / WSL)

As execuções registradas usaram Python 3.12.15 e TenSEAL 0.3.16.
Na raiz do projeto, para criar um ambiente novo:

```bash
conda create -n pir_songs python=3.12 pip
conda activate pir_songs
python -m pip install -r requirements-pir.txt
```

## Executar

Teste os seis registros com blocos de 1024 bytes (padrão):

```bash
python scripts/primeiro_teste.py
```

Para testar blocos de 4096 bytes:

```bash
python scripts/primeiro_teste.py --block-size 4096
```

Sem `--output-dir`, a pasta é definida automaticamente pelo tamanho do bloco:
`results/blocos_1024` ou `results/blocos_4096`.

O script mostra a associação índice/nome antes de executar. Com os seis nomes
utilizados no experimento, a ordem é 220, 330, 440, 523, 660 e 880 Hz.
Para testar somente o índice 2 (tom de 440 Hz nessa ordem):

```bash
python scripts/primeiro_teste.py --select 2 --output-dir results/selecionado_2
```

O índice `--select` fica no executor cliente. Ele não é argumento de `PIRServer.answer`.
O banco, o contexto público e os criptogramas de consulta são suficientes ao servidor.

Para recuperar o segundo bloco de cada áudio:

```bash
python scripts/primeiro_teste.py --block-index 1 --output-dir results/segundo_bloco
```

Para testar o último bloco parcial dos WAVs gerados (160000 bytes de áudio PCM):

```bash
python scripts/primeiro_teste.py --block-index 156 --output-dir results/ultimo_bloco
```

Nesse último caso, há 256 bytes válidos e 768 bytes de preenchimento por registro.
O preenchimento completa o registro com zeros. O script rejeita uma posição
de bloco que não exista em algum arquivo; padding de faixas inteiras ainda não foi implementado.

Para um caminho diferente ou tamanho de bloco diferente:

```bash
python scripts/primeiro_teste.py --samples /caminho/para/samples --block-size 512
```

`--block-size` admite de 1 a 4096 bytes. Com o padrão, 1024 bytes correspondem
a 32 ms de áudio mono, PCM de 16 bits, a 16000 amostras/s.
Com 4096 bytes, o trecho corresponde a 128 ms.

## Saída e critérios de sucesso

O terminal deve mostrar `OK` para cada registro e terminar com:

```text
Resultado: 6/6 recuperações corretas.
```

O programa retorna código 0 se todas as comparações passarem, 1 em caso de
divergência de bytes e 2 em caso de entrada ou dependência inválida.

São criados em `results/blocos_<tamanho>/` (ou na pasta definida por `--output-dir`):

- `resultado.json`: parâmetros, versões, tempos, tamanhos de mensagens e hashes SHA-256.
- `*_bloco_0000.bin`: blocos recuperados, sem cabeçalho WAV. Não são músicas reproduzíveis.

A comparação exata `recovered == expected` determina a correção. SHA-256 serve
para registrar o resultado; não é cifragem e não fornece privacidade à consulta.
Uma execução seguinte na mesma pasta substitui o relatório e os blocos de mesmo nome.
Use `--output-dir` para guardar experimentos separados.

## Resultados dos testes realizados

Foram executadas seis consultas por configuração, uma para cada áudio,
sempre sobre o primeiro bloco (`block_index = 0`).
Em ambas as configurações, todos os blocos foram recuperados exatamente,
sem preenchimento, e o contexto do servidor não continha chave secreta.

| Métrica por consulta | 1024 bytes | 4096 bytes |
| --- | ---: | ---: |
| Recuperações corretas | 6/6 | 6/6 |
| Áudio útil recuperado | 32 ms | 128 ms |
| Tempo total local médio | 24.34 ms | 26.67 ms |
| Consulta serializada (aproximadamente) | 531,3 kB | 531,2 kB |
| Resposta serializada (aproximadamente) | 88,5 kB | 88,6 kB |
| Consulta + resposta por byte útil | 605 bytes | 151 bytes |

Nesta tabela, kB corresponde a 1000 bytes. As médias usam as seis consultas de
cada relatório. A coluna de 1024 bytes corresponde à nova execução salva em
`blocos_1024`, cujo `client_setup_ms` é aproximadamente 40,83 ms;
a de 4096 bytes corresponde à execução com `client_setup_ms` de aproximadamente
28,45 ms. Os tempos de preparação não entram no tempo total médio por consulta.

Os relatórios devem ser mantidos em:

```text
results/
├── blocos_1024/
│   └── resultado.json
└── blocos_4096/
    └── resultado.json
```

Usar 4096 bytes permite recuperar quatro vezes mais dados com praticamente
o mesmo volume de mensagens, pois os parâmetros criptográficos e a quantidade
de criptogramas permanecem iguais. A sobrecarga continua elevada: para estas
bases pequenas, baixar todos os seis registros ainda custa menos comunicação.

Esses resultados são medições iniciais, não um benchmark estatístico.
Novas execuções podem produzir tempos e tamanhos serializados ligeiramente
diferentes; os bytes recuperados e seus hashes devem permanecer iguais para
os mesmos blocos de entrada. Atualize a tabela se substituir os relatórios.

Os JSONs podem ser versionados no Git. Os blocos `.bin` são gerados novamente
pelo script e podem ser ignorados, assim como ambientes locais e caches:

```gitignore
results/**/*.bin
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
venv/
.env
```

## Como o protocolo foi implementado

Para escolher a faixa de índice `x`, o cliente cria um seletor `s_i` por registro:

```text
s_i = 1 se i == x, e 0 nos outros casos.
q_i = Enc([s_i, s_i, ..., s_i])
```

O bit é repetido em B slots, onde B é o tamanho do bloco: 1024 ou 4096 nos testes realizados. Há um slot por byte. Para cada registro
`DB[i]`, o servidor multiplica o seletor cifrado pelo vetor de bytes em claro
e soma os resultados:

```text
resposta = Enc([0, ..., 0]) + soma_i(q_i * DB[i])
Dec(resposta) = DB[x]
```

A multiplicação é elemento a elemento, por meio do batching BFV. Portanto, ao
decifrar, cada slot contém o byte correspondente do registro escolhido.
A soma começa com uma cifragem de zero para permitir inclusive uma base toda
zerada. Os termos cujo registro público inteiro é zero são ignorados, evitando
criptogramas transparentes que a biblioteca pode rejeitar.

### Parâmetros

- Esquema: BFV.
- Grau do polinômio: 4096 (capacidade de até 4096 slots).
- Módulo de texto claro `t`: 65537.
- Módulos de coeficientes: valores padrão selecionados pelo SEAL para esse grau.
- Um byte por slot; os valores armazenados estão entre 0 e 255.
- Sem rotações e sem multiplicações entre dois criptogramas; não se exportam
  chaves de Galois nem de relinearização.
- Uma thread por contexto, para evitar variação de paralelismo neste teste.

Este código usa `t=65537`, um primo congruente a 1 módulo `2*4096`, para habilitar o batching
do BFV usado pelo TenSEAL. Não altere arbitrariamente os parâmetros para tentar
acomodar uma música inteira.

## Fronteira cliente/servidor e limites

Este experimento roda em um único processo Python com objetos de cliente e
servidor separados. Todos os seletores, o contexto público e a resposta são
serializados e reconstruídos, mas não há socket ou HTTP. É um teste local do
protocolo, não isolamento de memória entre processos.

O servidor rejeita um contexto que contenha chave secreta e não recebe o índice
escolhido. Os logs e o JSON com nomes, índices e resultados pertencem ao executor
de testes no lado cliente; não devem virar mensagens para o servidor real.

O modelo é de servidor honesto, mas curioso. Os testes checam correção e erros de
integração; não constituem prova de segurança criptográfica nem medem resistência
a todos os ataques. Não há autenticação de respostas de servidor malicioso.

O número de registros, o tamanho dos blocos e a posição do bloco são públicos.
A proteção do histórico de reprodução, horários, comprimentos de faixas e número
de blocos pedidos exige decisões adicionais na aplicação completa.

Os tempos incluem a serialização de consulta/resposta nos respectivos métodos,
mas não incluem rede ou escrita dos arquivos de resultado. Geração de chaves e
carregamento do contexto público são registrados separadamente. Os tamanhos
medidos são os payloads criptográficos, sem framing de transporte. Há apenas uma
execução por índice: estes números são diagnóstico inicial, não um benchmark
estatístico nem uma comparação com streaming convencional.

## Testes automatizados

```bash
python -m unittest discover -s tests -v
```

Cobrem todos os índices, bytes extremos, base zerada, resposta com um criptograma,
nova aleatoriedade por consulta, ausência de chave secreta no servidor, capacidade
máxima de 4096 bytes e leitura correta do último bloco de um WAV.

## Referências

- Survey utilizado no TCC: https://eprint.iacr.org/2026/1135
- TenSEAL: https://github.com/OpenMined/TenSEAL
- Versão utilizada: https://pypi.org/project/tenseal/0.3.16/
- Batching BFV/BGV: https://github.com/microsoft/SEAL/blob/main/native/examples/2_encoders.cpp




