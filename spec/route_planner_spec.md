# Spec: `route_planner` — Rota com Pontos de Parada e Interesse

> versão 2.0

## Visão Geral

Skill que recebe um pedido de rota em linguagem natural (via WhatsApp) e retorna um itinerário detalhado com pontos de parada sugeridos, tempo estimado e pontos de interesse ao longo do caminho.

Suporta waypoints fixos obrigatórios definidos pelo usuário, pontos de interesse específicos com tolerância de desvio configurável, e planejamento independente de abastecimento com base na autonomia do veículo.

---

## Entrada

### Mensagem do usuário (exemplos)

```
"Vou de São Paulo para Florianópolis de carro. Me sugere paradas no caminho."
"Rota de BH até o Rio com pontos turísticos e bons restaurantes."
"Quero parar de 2 em 2 horas, saindo de Curitiba para Porto Alegre."
"Saindo de SP para Flo, quero passar obrigatoriamente por Curitiba e parar no Parque Vila Velha."
"Minha moto tem autonomia de ~280km, não me deixa ficar sem posto."
```

### `args` esperados pelo planner

```json
{
  "origin": "São Paulo, SP",
  "destination": "Florianópolis, SC",
  "mode": "car",
  "stop_interval_km": 150,
  "stop_interval_hours": null,
  "preferences": ["restaurante", "visual panorâmico"],
  "max_stops": 4,

  "fixed_waypoints": [
    "Curitiba, PR",
    "Joinville, SC"
  ],

  "fixed_pois": [
    {
      "name": "Parque Estadual de Vila Velha",
      "location": "Ponta Grossa, PR",
      "max_detour_km": 20
    }
  ],

  "fuel": {
    "enabled": true,
    "max_interval_km": 220,
    "tank_km_remaining": 280,
    "preferred_brands": ["BR", "Ipiranga"]
  }
}
```

#### Campos — referência completa

| Campo | Tipo | Obrigatório | Default | Descrição |
|---|---|---|---|---|
| `origin` | string | ✅ | — | Endereço ou cidade de partida |
| `destination` | string | ✅ | — | Endereço ou cidade de destino |
| `mode` | string | — | `"car"` | Meio de transporte: `car`, `motorcycle`, `truck` |
| `stop_interval_km` | int | — | 150 | Distância entre paradas de descanso sugeridas. Mutuamente exclusivo com `stop_interval_hours` |
| `stop_interval_hours` | float | — | `null` | Intervalo de tempo entre paradas (ex: `2.0` para "de 2 em 2 horas"). Se informado, sobrepõe `stop_interval_km` |
| `preferences` | string[] | — | `[]` | Categorias de POI desejadas nas paradas (restaurante, hotel, farmácia, etc.) |
| `max_stops` | int | — | 4 | Limite de paradas sugeridas (não conta waypoints fixos) |
| `fixed_waypoints` | string[] | — | `[]` | Cidades ou endereços pelos quais o usuário **obrigatoriamente** quer passar, em ordem |
| `fixed_pois` | object[] | — | `[]` | Pontos de interesse específicos a incluir, com tolerância de desvio |
| `fixed_pois[].name` | string | ✅ | — | Nome do local |
| `fixed_pois[].location` | string | ✅ | — | Endereço ou cidade de referência |
| `fixed_pois[].max_detour_km` | int | — | 15 | Desvio máximo aceitável em km para incluir o ponto |
| `fuel.enabled` | bool | — | `false` | Ativa o planejamento de abastecimento |
| `fuel.max_interval_km` | int | — | 200 | Distância máxima entre postos garantidos na rota |
| `fuel.tank_km_remaining` | int | — | — | Autonomia estimada atual (km restantes no tanque ao partir) |
| `fuel.preferred_brands` | string[] | — | `[]` | Bandeiras preferidas (BR, Shell, Ipiranga, etc.) |

### `RequestContext` utilizado

| Campo | Uso |
|---|---|
| `user_text` | Mensagem original para extração de parâmetros |
| `history` | Contexto para inferir origem/destino omitidos |
| `sender_id` | Personalização futura (preferências salvas por usuário) |

---

## Processamento Interno

```
Planner seleciona route_planner
        ↓
RouteplannerSkill.run(context, args)
        ↓
1. Geocodificação de origem, destino, fixed_waypoints e fixed_pois
   → Nominatim (OpenStreetMap) — sem API key
        ↓
2. Cálculo de rota com waypoints fixos obrigatórios
   → OSRM (open-source) — sem API key
   → Rota respeita a ordem: origin → fixed_waypoints[] → destination
   → Extrai pontos a cada stop_interval_km nos segmentos livres
        ↓
3. Inserção de fixed_pois
   → Para cada fixed_poi, verifica desvio real vs. max_detour_km
   → Se dentro do limite: insere na posição correta da rota
   → Se fora do limite: notifica usuário e omite (não quebra a rota)
        ↓
4. Planejamento de abastecimento (se fuel.enabled)
   → Calcula autonomia restante a cada waypoint
   → Identifica segmentos onde autonomia cai abaixo de fuel.max_interval_km
   → Busca postos via Overpass API nos pontos críticos
   → Garante ao menos 1 posto verificado antes de cada zona de risco
   → Paradas de abastecimento são marcadas com tipo "fuel" (distintas das de descanso)
        ↓
5. Busca de POIs por segmento
   → Overpass API (OpenStreetMap) — sem API key
   → Filtra por categorias em preferences
   → Aplica preferred_brands para postos quando relevante
        ↓
6. Montagem do itinerário estruturado
   → Ordena todas as paradas por posição na rota:
     fixed_waypoints + fixed_pois + paradas sugeridas + paradas de abastecimento
   → Enriquece com nome, tipo, distância acumulada, tempo estimado, desvio (se aplicável)
        ↓
7. LLM formata resposta em linguagem natural
   → executor.py chama interpret_result()
   → Saída adaptada ao WhatsApp, com distinção visual entre tipos de parada

### Tratamento de erros

| Situação | Comportamento |
|---|---|
| Geocodificação falha (origem ou destino) | `success=False`, mensagem pedindo endereço mais específico |
| Rota não encontrada entre os pontos | `success=False`, sugere verificar nome das cidades |
| `fixed_poi` excede `max_detour_km` | Omite o POI, registra em `fixed_pois_omitted`, avisa o usuário na resposta |
| Overpass API indisponível | Continua sem POIs, informa que sugestões de interesse não estão disponíveis |
| `stop_interval_km` e `stop_interval_hours` ambos preenchidos | `stop_interval_hours` tem precedência; planner deve evitar enviar os dois |
```

---

## Tipos de Parada

| Tipo | Origem | Símbolo sugerido |
|---|---|---|
| `waypoint_fixed` | Definido pelo usuário via `fixed_waypoints` | 📌 |
| `poi_fixed` | Definido pelo usuário via `fixed_pois` | ⭐ |
| `rest` | Sugerido pelo planner via `stop_interval_km` | 🛑 |
| `fuel` | Gerado pelo módulo de abastecimento | ⛽ |

Paradas do tipo `fuel` nunca são omitidas nem submetidas ao limite `max_stops`. São restrições hard do itinerário.

---

## Saída

### Resposta no WhatsApp

```
🗺️ *Rota: São Paulo → Florianópolis*
Distância total: ~720 km | Tempo estimado: ~8h

📌 *Waypoint fixo — km 400 | ~4h de SP*
📍 Curitiba, PR
• 🍽️ Restaurante Schwambach (churrascaria, ⭐ 4.5)
• 🏞️ Jardim Botânico de Curitiba (15 min desvio)

⭐ *Ponto de interesse — km 460 | ~4h50 de SP*
📍 Parque Estadual de Vila Velha, Ponta Grossa, PR
↪️ Desvio: ~12 km da rota principal

⛽ *Abastecimento — km 520 | ~5h30 de SP*
📍 Posto BR — Rodovia BR-376, Tijucas do Sul, PR
⚠️ Próximo trecho sem postos verificados: 140 km

🛑 *Parada sugerida — km 580 | ~6h10 de SP*
📍 Joinville, SC
• 🍽️ Café Colonial Gramado Center
• ⛽ Ipiranga Rodovia BR-101

⛽ *Abastecimento — km 660 | ~7h de SP*
📍 Posto Ipiranga — BR-101, Itajaí, SC

🏁 *Florianópolis* — chegada estimada em ~8h

Quer ajustar alguma parada, adicionar um ponto fixo ou mudar o intervalo de abastecimento?
```

### `SkillResult` retornado

```python
SkillResult(
    success=True,
    data={
        "route": {...},         # dados brutos da rota (waypoints, distâncias)
        "stops": [
            {
                "type": "waypoint_fixed",   # ou poi_fixed, rest, fuel
                "name": "Curitiba, PR",
                "km_from_origin": 400,
                "eta_minutes": 240,
                "detour_km": None,
                "pois": [...],
            },
            {
                "type": "poi_fixed",
                "name": "Parque Estadual de Vila Velha",
                "km_from_origin": 460,
                "eta_minutes": 290,
                "detour_km": 12,
                "pois": [],
            },
            {
                "type": "fuel",
                "name": "Posto BR — BR-376, Tijucas do Sul, PR",
                "km_from_origin": 520,
                "eta_minutes": 330,
                "detour_km": 0,
                "pois": [],
            },
            ...
        ],
        "total_km": 720,
        "estimated_hours": 8,
        "fuel_stops_count": 2,
        "fixed_pois_omitted": []    # lista de POIs recusados por exceder max_detour_km
    },
    summary="Rota SP→Floripa com 2 waypoints fixos, 1 POI e 2 abastecimentos gerada com sucesso.",
    display_text="🗺️ *Rota: São Paulo → Florianópolis*\n..."
)
```

---

## Arquitetura da Skill

**Arquivo:** `skills/route_planner.py`

```
RouteplannerSkill(BaseSkill)
├── name = "route_planner"
├── description = "Planeja rotas com waypoints fixos, POIs específicos, paradas de descanso e abastecimento"
├── planner_visible = True
│
├── _geocode(location: str) → (lat, lon)
├── _get_route(origin, destination, mode, fixed_waypoints) → waypoints[]
│
├── _insert_fixed_pois(waypoints, fixed_pois) → (waypoints[], omitted[])
│   └── Verifica desvio real e insere ou descarta cada POI
│
├── _plan_fuel_stops(waypoints, fuel_args) → fuel_stops[]
│   ├── Simula consumo de autonomia ao longo da rota
│   ├── Detecta segmentos críticos
│   └── Busca postos via Overpass e insere paradas hard
│
├── _get_pois(lat, lon, radius_m, preferences) → pois[]
├── _build_itinerary(waypoints, fuel_stops, pois_per_stop) → stops[]
│   └── Merge e ordena todos os tipos de parada por km_from_origin
│
└── run(context: RequestContext, args: dict) → SkillResult
```

---

## Dependências Externas

| Serviço | Uso | API Key? |
|---|---|---|
| Nominatim (OSM) | Geocodificação de todos os pontos | Não |
| OSRM | Cálculo de rota, waypoints, desvios | Não |
| Overpass API | POIs e postos de combustível | Não |
| Google Maps APIs | Alternativa premium (geocoding + places) | Sim |

Preferir APIs sem custo. Google Maps como fallback configurável via `.env`.

---

## Integração com Runtime

| Componente | Impacto |
|---|---|
| `planner.py` | Nenhum — seleciona skill por descrição via LLM |
| `executor.py` | Nenhum — consome `SkillResult` padrão |
| `skills/registry.py` | Registrar `RouteplannerSkill` na lista de skills |
| `context_builder.py` | Nenhum — `user_text` já disponível |
| `session_store.py` | Opcional: persistir rota para follow-up conversacional |

---

## Extensões Futuras

- **Follow-up conversacional:** refinamento de paradas usando histórico de sessão ("tira restaurantes, só postos")
- **Perfil de veículo salvo:** autonomia, consumo médio e bandeiras preferidas persistidos por `sender_id` (ex: "minha Scrambler")
- **Integração Garmin:** ajustar intervalos de parada com base em condicionamento físico do usuário
- **Link Google Maps:** gerar URL com waypoints pré-carregados, incluindo postos de abastecimento
- **Alertas via n8n:** notificar antes da viagem com previsão do tempo nas paradas
- **Cálculo de custo de combustível:** estimar gasto com base no preço médio por bandeira na região