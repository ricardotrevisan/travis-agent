# Spec: `route_planner` — Rota com Pontos de Parada e Interesse

> versão 3.0

## Visão Geral

Skill que recebe um pedido de rota em linguagem natural (via WhatsApp) e retorna um itinerário detalhado com pontos de parada sugeridos, tempo estimado e pontos de interesse ao longo do caminho.

Suporta waypoints fixos obrigatórios, planejamento de abastecimento baseado em autonomia do veículo, e busca curada de pontos de interesse ao longo de toda a polyline da rota com filtragem por nota, número de avaliações e desvio máximo por categoria.

Os dois fluxos são independentes: calcular a rota não implica buscar POIs, e buscar POIs não recalcula a rota. O usuário aciona cada um separadamente.

---

## Ações disponíveis

| `action` | Descrição |
|---|---|
| `plan` (padrão) | Calcula a rota e salva no Redis. TTL: 30 min, renovado a cada recalculo. |
| `gpx` | Gera o GPX da última rota salva no Redis e sobe para o Drive. |
| `poi_search` | Varre a polyline da rota salva e devolve lista curada de pontos de interesse. |
| `add_pois` | Insere POIs escolhidos pelo usuário na rota salva e devolve itinerário atualizado. |

O TTL de 30 minutos é renovado automaticamente a cada `plan` ou `add_pois` (ambos chamam `_save_last_route`, que executa `SET … EX`). O fluxo `plan → poi_search → add_pois` deve ocorrer dentro desse janela; se o cache expirar, o usuário precisa refazer a rota.

---

## Entrada

### Mensagem do usuário (exemplos)

```
"Vou de São Paulo para Florianópolis de carro. Me sugere paradas no caminho."
"Rota de BH até o Rio com pontos turísticos e bons restaurantes."
"Quero parar de 2 em 2 horas, saindo de Curitiba para Porto Alegre."
"Saindo de SP para Flo, quero passar obrigatoriamente por Curitiba e parar no Parque Vila Velha."
"Minha moto tem autonomia de ~280km, não me deixa ficar sem posto."
"Quais pontos de interesse tem ao longo dessa rota?"
"Adiciona a Cachoeira do Escorrega na rota."
```

### `args` esperados pelo planner — `action=plan`

```json
{
  "action": "plan",
  "origin": "São Paulo, SP",
  "destination": "Florianópolis, SC",
  "stop_interval_km": 150,
  "stop_interval_hours": null,
  "max_stops": 4,
  "fixed_waypoints": ["Curitiba, PR", "Joinville, SC"],
  "fuel": {
    "enabled": true,
    "max_interval_km": 220,
    "tank_km_remaining": 280,
    "preferred_brands": ["BR", "Ipiranga"]
  }
}
```

> `fixed_pois` e `preferences` foram removidos do `plan`. POIs são adicionados exclusivamente via `poi_search` + `add_pois`.

### `args` esperados pelo planner — `action=poi_search`

```json
{
  "action": "poi_search",
  "categories": ["natureza", "gastronomia regional", "cultura", "adrenalina"],
  "sample_interval_km": 20
}
```

`poi_search` não recebe origem/destino — recupera a rota do Redis pelo `sender`. Se não houver rota cacheada, retorna erro orientando o usuário a calcular a rota primeiro.

### `args` esperados pelo planner — `action=add_pois`

```json
{
  "action": "add_pois",
  "pois": [
    {
      "place_id": "ChIJ...",
      "name": "Cachoeira do Escorrega",
      "lat": -23.456,
      "lon": -46.789,
      "category": "natureza",
      "max_detour_km": 12
    }
  ]
}
```

`add_pois` carrega a rota do Redis, insere os POIs como `poi_fixed`, reordena os stops por `km_from_origin`, salva de volta no Redis (renovando o TTL) e devolve o itinerário atualizado.

#### Campos — referência completa (`action=plan`)

| Campo | Tipo | Obrigatório | Default | Descrição |
|---|---|---|---|---|
| `origin` | string | ✅ | — | Endereço ou cidade de partida |
| `destination` | string | ✅ | — | Endereço ou cidade de destino |
| `stop_interval_km` | int | — | 150 | Distância entre paradas de descanso sugeridas |
| `stop_interval_hours` | float | — | `null` | Intervalo de tempo entre paradas. Sobrepõe `stop_interval_km` se informado |
| `max_stops` | int | — | 4 | Limite de paradas sugeridas (não conta waypoints fixos nem combustível) |
| `fixed_waypoints` | string[] | — | `[]` | Cidades ou endereços obrigatórios, em ordem |
| `fuel.enabled` | bool | — | `false` | Ativa planejamento de abastecimento |
| `fuel.max_interval_km` | int | — | 200 | Distância máxima entre postos garantidos |
| `fuel.tank_km_remaining` | int | — | — | Autonomia ao partir (km) |
| `fuel.preferred_brands` | string[] | — | `[]` | Bandeiras preferidas (BR, Shell, Ipiranga, etc.) |

---

## Processamento Interno

### `action=plan`

```
1. Geocodificação de origem, destino e fixed_waypoints
   → Google Geocoding API

2. Cálculo de rota com waypoints fixos obrigatórios
   → OSRM público (perfil driving)
   → Extrai polyline completa, total_km, total_minutes

3. Planejamento de abastecimento (se fuel.enabled)
   → Varre polyline a cada 5km buscando postos (Google Places, raio 2km)
   → Prioriza bandeiras conhecidas; fallback valida via Place Details
   → Desvio máximo via OSRM: 3km

4. Paradas de descanso
   → sample_waypoints() distribui paradas pelo intervalo configurado
   → Apenas nome da localidade via reverse geocode — sem busca de POIs

5. Montagem e ordenação do itinerário
   → Merge de fixed_waypoints + rest + fuel por km_from_origin

6. Persiste no Redis: polyline + stops + origin + destination
   → TTL: 30 min (renovado a cada chamada _save_last_route)
```

### `action=poi_search`

```
1. Carrega rota do Redis (polyline, total_km, total_minutes)
   → Erro se não encontrar

2. Amostra a polyline a cada sample_interval_km (default: 20km)
   → point_at_km() para cada posição amostrada

3. Para cada ponto amostrado:
   a. Detecta contexto do trecho via velocidade média do segmento:
      → avg_speed > 90 km/h  → rodovia rápida  → raio 8km
      → avg_speed 50–90 km/h → trecho misto     → raio 15km
      → avg_speed < 50 km/h  → perímetro urbano → raio 5km

   b. Busca Google Places para cada categoria solicitada
      → Expõe rating e user_ratings_total no retorno

4. Deduplicação por place_id (um mesmo lugar pode aparecer em múltiplos raios)

5. Filtro de qualidade por categoria:
   → Natureza/paisagem:       nota ≥ 4.4, avaliações ≥ 50
   → Gastronomia regional:    nota ≥ 4.4, avaliações ≥ 50
   → Cultura/história:        nota ≥ 4.0, avaliações ≥ 20 (único na categoria aceito com menos)
   → Adrenalina:              nota ≥ 4.4, avaliações ≥ 50

6. Filtro de desvio máximo por categoria:
   → Mirante, cachoeira (parada rápida):  ≤ 12km da rota
   → Almoço, museu (parada com tempo):    ≤ 6km da rota
   → Ponto âncora (destino temático):     ≤ 20km da rota

7. Formata lista numerada para o usuário escolher
   → Ordenado por km_from_origin
   → Inclui: nome, categoria, nota, nº avaliações, desvio estimado, km na rota
```

### `action=add_pois`

```
1. Carrega rota do Redis

2. Para cada POI recebido:
   → Calcula km_from_origin via _closest_km()
   → Calcula eta_minutes via _eta()
   → Verifica desvio real via driving_distance_m() vs. max_detour_km do POI
   → Se dentro do limite: insere como poi_fixed
   → Se fora: notifica e omite

3. Reordena todos os stops por km_from_origin

4. Salva rota atualizada no Redis (renova TTL de 30 min)

5. Retorna itinerário completo atualizado (mesmo formato do action=plan)
```

---

## Raio de busca dinâmico

O raio é determinado pela velocidade média do trecho entre o ponto amostrado anterior e o atual, derivada da polyline OSRM (total_km / total_minutes por segmento):

| Contexto detectado | Critério | Raio |
|---|---|---|
| Rodovia rápida | avg_speed > 90 km/h | 8km |
| Trecho misto / estrada secundária | 50–90 km/h | 15km |
| Perímetro urbano | < 50 km/h | 5km |

O raio de 15km cobre desvios reais de moto em estrada sem transformar a parada em outro roteiro. Em rodovias, 8km porque 15km em cada direção já seria um sub-trajeto independente.

---

## Categorias de POI e mapeamento Google Places

| Categoria (input) | Google Places type | Ícone |
|---|---|---|
| `natureza` | `natural_feature` | 🌿 |
| `mirante` | `natural_feature` | 🏞️ |
| `gastronomia regional` | `restaurant`, `cafe` | 🍽️ |
| `cultura` | `museum` | 🏛️ |
| `adrenalina` | `amusement_park`, `stadium` | 🏁 |
| `restaurante` | `restaurant` | 🍽️ |
| `cafe` | `cafe` | ☕ |
| `museu` | `museum` | 🏛️ |
| `parque` | `park` | 🌳 |
| `posto` | `gas_station` | ⛽ |
| `hotel` | `lodging` | 🏨 |
| `farmacia` | `pharmacy` | 💊 |

Categorias **fora do escopo** do perfil moto solo exploratório (não devem ser sugeridas pelo planner): shoppings, parques aquáticos, atrações infantis, hotéis (exceto se solicitado explicitamente).

---

## Critérios de qualidade por categoria

| Categoria | Nota mínima | Avaliações mínimas | Exceção |
|---|---|---|---|
| Natureza/paisagem | ≥ 4.4 | ≥ 50 | — |
| Gastronomia regional | ≥ 4.4 | ≥ 50 | — |
| Cultura/história | ≥ 4.0 | ≥ 20 | Único representante na cidade entra com esses critérios já flexibilizados |
| Adrenalina | ≥ 4.4 | ≥ 50 | — |

---

## Desvio máximo por tipo de parada

| Tipo de parada | Desvio máximo da rota |
|---|---|
| Mirante, cachoeira (parada rápida, sem reserva) | 12km |
| Almoço, museu (parada com tempo) | 6km |
| Ponto âncora (destino com identidade própria) | 20km |
| Abastecimento | 3km (via OSRM) |

---

## Tipos de Parada

| Tipo | Origem | Símbolo |
|---|---|---|
| `waypoint_fixed` | Definido pelo usuário via `fixed_waypoints` | 📌 |
| `poi_fixed` | Definido pelo usuário via `fixed_pois` ou `add_pois` | ⭐ |
| `rest` | Sugerido pelo planner via `stop_interval_km` | 🛑 |
| `fuel` | Gerado pelo módulo de abastecimento | ⛽ |

Paradas do tipo `fuel` nunca são omitidas nem submetidas ao limite `max_stops`.

---

## Cache Redis

| Chave | Conteúdo | TTL |
|---|---|---|
| `agent:route:last:{sender}` | `{origin, destination, stops, coordinates}` | 30 min |

O TTL é renovado automaticamente a cada `SET` (ações `plan` e `add_pois`). O fluxo `plan → poi_search → add_pois` deve ocorrer dentro dessa janela. Se o cache expirar, o usuário precisa refazer a rota — a skill orienta com mensagem clara.

---

## Saída

### Resposta — `action=poi_search`

```
🔍 *Pontos de interesse ao longo da rota SP → Floripa*

🌿 *Natureza*
1. Cachoeira do Avencal — km 480 | desvio ~4km | ⭐ 4.7 (312 avaliações)
2. Mirante da Serra do Mar — km 310 | desvio ~8km | ⭐ 4.5 (89 avaliações)

🍽️ *Gastronomia regional*
3. Café Colonial Witmarsun — km 415 | desvio ~2km | ⭐ 4.6 (540 avaliações)
4. Boteco do Zé da Roça — km 590 | desvio ~1km | ⭐ 4.4 (73 avaliações)

🏛️ *Cultura*
5. Museu Histórico de Lapa — km 395 | desvio ~6km | ⭐ 4.3 (28 avaliações)

Quais quer adicionar à rota? Responda com os números (ex: "1, 3 e 5") ou me diga quais não te interessam.
```

### Resposta — `action=add_pois` / `action=plan`

Mesmo formato existente do `_format_whatsapp`, com POIs adicionados como paradas `⭐ Ponto de interesse`.

### `SkillResult` — `action=poi_search`

```python
SkillResult(
    ok=True,
    output={
        "candidates": [
            {
                "place_id": "ChIJ...",
                "name": "Cachoeira do Avencal",
                "category": "natureza",
                "lat": -26.234,
                "lon": -49.123,
                "km_from_origin": 480,
                "detour_km": 4.1,
                "rating": 4.7,
                "user_ratings_total": 312,
            },
            ...
        ]
    },
    user_visible_text="🔍 *Pontos de interesse...*"
)
```

---

## Tratamento de erros

| Situação | Comportamento |
|---|---|
| `poi_search` sem rota no Redis | `ok=False`, orienta refazer a rota |
| `add_pois` sem rota no Redis | `ok=False`, orienta refazer a rota |
| POI excede desvio máximo | Omite, notifica na resposta |
| Google Places indisponível | Continua sem POIs, informa na resposta |
| Geocodificação falha (origin/destination) | `ok=False`, pede nome mais específico |
| `fixed_waypoint` não geocodifica | `ok=False`, aborta rota inteira |

---

## Mudanças necessárias para implementação

### `utils/geo_client.py`

- Expor `rating` e `user_ratings_total` no dict de retorno de `get_pois` (campos já presentes na resposta da Places API)
- Expandir `_CATEGORY_PLACES` com:
  - `"natureza"` → `"natural_feature"`
  - `"gastronomia regional"` → `"restaurant"`
  - `"adrenalina"` → `"amusement_park"`

### `skills/route_planner.py`

- Constante `_POI_QUALITY_PROFILE`: nota mínima e reviews mínimos por categoria
- Constante `_POI_DETOUR_LIMITS`: desvio máximo por tipo de parada
- Função `_poi_search_radius(avg_speed_kmh) → int`: raio em metros por contexto
- Função `_search_pois_along_route(coordinates, total_km, total_minutes, categories, sample_interval_km) → list[dict]`: varre polyline, deduplica por `place_id`, filtra qualidade e desvio
- Função `_format_poi_candidates(candidates) → str`: lista numerada para o usuário
- Branch `action=poi_search` em `run()`
- Branch `action=add_pois` em `run()`

### `spec/route_planner_spec.md`

- Este documento (versão 3.0)

---

## Dependências Externas

| Serviço | Uso | API Key? |
|---|---|---|
| Google Geocoding API | Geocodificação de todos os pontos | Sim |
| OSRM (público) | Cálculo de rota, desvio driving, polyline | Não |
| Google Places API | POIs, postos, rating, user_ratings_total | Sim |
| Google Drive API (via MCP) | Upload do GPX | Sim |
| Redis | Cache da rota entre ações conversacionais | Não (interno) |

---

## Extensões Futuras

- **Horário de funcionamento:** Places API tem `opening_hours`; lógica de sequência temporal (museu de manhã, natureza à tarde) é uma feature à parte
- **Sinalização de reserva obrigatória:** derivável de texto de reviews; sem campo direto na API
- **Estacionamento para moto:** `parkingOptions` disponível na Nova Places API v1 (endpoint diferente do atual)
- **Perfil de veículo salvo:** autonomia, consumo e bandeiras preferidas persistidos por `sender_id`
- **Alertas pré-viagem:** notificação n8n com previsão do tempo nas paradas
