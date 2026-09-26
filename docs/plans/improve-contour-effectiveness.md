# Контур улучшения BPMN: эффективность выявления проблем — Implementation plan

**Goal:** закрыть все дефекты контура улучшения (P0/P1/P2 из адверсариального ревью 2026-09-24), починить слой измер так, чтобы `improve/*` считались по фактам, а не по выводам, и поднять способность контура находить проблемы схемы — и по корректности ноты BPMN, и по бизнес-логике процесса.

**Architecture:** три опоры. (1) Единая точка истины «пакет применён и не ухудшил схему»: `bpmn_edits.apply_and_guarantee()` с инъективным предикатом `reject`, которую зовут и оркестратор, и харнесс, — расхождение replay/продукт исчезает по построению, а вместе с ним класс дефектов «отчёт враньё после отката». (2) Метрики только на фактах из отчёта (`retry_attempted`, `package_reverted`, `rules_regressed`) с честным `None` вместо 0.0 там, где данных нет. (3) Бизнес-слой: скоринг получает исполнимое действие на каждое правило и новые правила о логике процесса, а независимый оракул `eval/invariants.py` получает свои проверки тех же свойств — иначе «улучшение» меряется тем же скорингом, который он оптимизирует.

**Tech Stack:** Python 3.12, `xml.etree`, pytest, sklearn/scipy (RAG, не трогаем), GigaChat-транспорт `core/llm_client.py` (не трогаем), React-фронт в `bpmn-constructor/` (не трогаем — чужая сессия).

**Порядок и параллелизация.** WP-A/E можно делать параллельно с WP-B/C/D: интерфейсы зафиксированы ниже и менять их нельзя без правки этого файла. Порядок слияния: **B → C → A → D → E → F**, потому что метрики A читают поля отчёта, которые появляются в C, а те — сигнатуру из B.

> Про коммиты: в этом репозитории коммиты не создаются без явной просьбы (см. память сессии). Каждый «Checkpoint» — это прогон тестов и `git diff --stat`, а не `git commit`.

---

## Контракты (фиксированы, все WP от них зависят)

```python
# core/bpmn_edits.py — новый публичный вход
GuaranteeReject = Callable[[str], Optional[str]]

def apply_and_guarantee(
        xml_text: str,
        operations: List[Dict[str, Any]],
        reject: "GuaranteeReject" = None,
) -> Tuple[str, Dict[str, Any]]:
    """Пакет операций + починка + гаранты «не хуже исходной».

    report = отчёт apply_operations плюс:
      "notes":         накопленные пометки всех прогонов починки,
      "reverted":      "" | причина откатить весь пакет,
      "noop_rows":     сколько «применённых» строк ничего не изменили,
      "repair_rounds": сколько раз прогнали validate_and_repair.
    reject(candidate_xml) -> str | None — причина откатить пакет целиком.
    При откате ВСЕ applied-строки переезжают в skipped с причиной отката:
    отчёт не может утверждать, что правка в схеме, если схема вернулась к базе.
    """

def merge_notes(*groups: Sequence[str]) -> List[str]:
    """Пометки по раундам починки с сохранением порядка и дедупликацией.
    Нужен потому, что неидемпотентные пометки (понижение шлюза до задачи,
    снятие default) второй вызов `validate_and_repair` не вернёт уже по тому,
    что чинить нечего."""

NOOP_NOTE_MARKERS = ("добавлено не было", "схема не изменилась")
```

```python
# core/bpmn_scoring.py — исполнимое действие у каждого правила
'rules': {'naming': {'weight': 10, 'message': '...', 'action': 'rename'}}
```
Инвариант, который держится тестом: **каждое проваленное правило рекомендацией
называет операцию из `bpmn_edits.OP_SPEC`**. Правило без исполнимого действия —
жалоба, которую пакет не выражает; правило с именем участника в тексте — утечка
в eval (см. WP-E).

```python
# core/llm_improve.py — новые поля отчёта (факты, а не вывода)
report["retry_attempted"]  # bool: корректирующий вызов реально состоялся
report["retry_closed"]     # int:  сколько отказов первого раунда закрыто
report["package_reverted"] # str:  "" | причина откатить пакет целиком
report["rules_regressed"]  # dict: {правило: "passed->failed"} у принятой схемы
report["noop_rows"]        # int:  из bpmn_edits.apply_and_guarantee
```

---

## WP-A. Слой измер: детектор регрессий, метрики улучшения, выгрузка

### Task A1. Нулевой baseline: направление «лучше» было перепутано местами (P0-1)

**Files:** `eval/metrics.py:268-277`, `tests/test_eval_harness.py:396-405`

- [ ] **Step 1: тест, который краснеет**

```python
def test_zero_baseline_respects_direction_not_its_inverse():
    """Метрика «меньше — лучше» с нулевым baseline: появление значения и есть
    ухудшение. Метрика «больше — лучше» с нулевым baseline: рост — улучшение,
    и объявлять его регрессией нельзя (иначе CI забракуют настоящий фикс
    improve/defects_repaired)."""
    lower = metrics.RegressionDetector(directions={"err": metrics.LOWER})
    assert [r.name for r in lower.compare({"err": 0.0}, {"err": 0.5})] == ["err"]
    higher = metrics.RegressionDetector(directions={"fixed": metrics.HIGHER})
    assert higher.compare({"fixed": 0.0}, {"fixed": 0.5}) == []
    assert [r.name for r in higher.compare({"fixed": 0.0}, {"fixed": -0.5})] == ["fixed"]
```

- [ ] **Step 2:** `python -X utf8 -m pytest tests/test_eval_harness.py -q -k zero_baseline` → FAIL на обеих ветках (сейчас `lower` молчит, `higher` кричит).

- [ ] **Step 3: правка**

```python
            if base == 0.0:
                # ноль в baseline: относительная доля бессмысленна, но знак
                # изменения читается: для LOWER плохо любое появление значения,
                # для HIGHER — только уход в минус.
                worse = ((now > 0.0) if self.direction_of(name) == LOWER
                         else (now < 0.0))
                if worse:
                    out.append(Regression(name=name, baseline=base, current=now,
                                          rel_change=1.0, threshold=self.threshold,
                                          direction=self.direction_of(name)))
                continue
```

- [ ] **Step 4: починить тест, который держал ошибку.** В `tests/test_eval_harness.py:404-405` заменить `RegressionDetector()` на `RegressionDetector(directions={"x": metrics.LOWER})` и комментарием пометить, что без направления вывод был бы неверным.
- [ ] **Step 5: Checkpoint** — `python -X utf8 -m pytest tests/test_eval_harness.py -q`.

### Task A2. Метрика, которой нет в baseline, не сверяется молча (P2)

**Files:** `eval/metrics.py:256-286`, `eval/harness.py:922-934`, `tests/test_eval_harness.py`

- [ ] **Step 1: тест** — новая метрика в прогоне при отсутствии её в baseline обязана попасть в `unverified`, а не исчезнуть:

```python
def test_new_metric_without_baseline_is_reported_as_unverified():
    d = metrics.RegressionDetector()
    assert d.compare({"score": 90.0}, {"score": 90.0, "improve/new": 0.0}) == []
    assert d.unverified({"score": 90.0}, {"score": 90.0, "improve/new": 0.0}) == ["improve/new"]
```

- [ ] **Step 2:** FAIL (`no attribute unverified`).
- [ ] **Step 3:** метод `unverified(baseline, current)` → отсортированные имена из `current`, которых нет в `baseline` или где `None` с одной стороны. В `harness.detect_regressions` — не возвращать их, а в `RunReport.baseline_note` дописывать: `«не сверялись с baseline: <имена> — baseline старее метрик»`.
- [ ] **Step 4:** PASS. **Step 5: Checkpoint** + `python -X utf8 -m eval.run --mode replay --no-report` (в выводе должен появиться список из снятых метрик, в т.ч. `improve/defects_repaired`).

### Task A3. `improve/retry_needed_share` выводилась из стадии отказа, а не из факта повтора (P0 метрики)

**Files:** `eval/harness.py:594-613, 317-340, 691-716`, `tests/test_eval_harness.py`

Метрика подменена целиком, потому что вывод `retried = any(stage=="retry")` даёт ложный ноль ровно в трёх случаях: повтор отработал без новых отказов; повтор вернулся с пустым `operations`; после повтора сработал гарант `stage="repair"`. Плюс битые кейсы добавляли 0.0 в знаменатель.

- [ ] **Step 1: тест на честность `None`**

```python
def test_improve_metrics_do_not_invent_zero_for_broken_cases():
    case = harness.ImproveCase(scenario="s", fixture="f", label="", quality="",
                               mode="live", error="нет схемы")
    suite = harness.build_improvement_suite()
    res = suite.run([{"name": "s/f", "payload": case}])
    for name, m in res.metrics.items():
        assert m.n == 0, f"{name} посчитана по кейсу без данных (n={m.n})"
```

- [ ] **Step 2: замена полей случая.** В `ImproveCase` добавить `retry_attempted: Optional[bool] = None`, `retry_closed: Optional[int] = None`, `package_reverted: str = ""`, `rules_regressed: Optional[Dict[str, str]] = None`, `noop_rows: Optional[int] = None`, `plan_truncated: Optional[int] = None`, `analysis: str = ""`. Поле `retried` удалить вместе с его выводом из стадий.
- [ ] **Step 3: live-ветка читает факты, а не выводит:**

```python
    report = report or {}
    return (xml_after or "", list(report.get("applied") or []),
            [dict(s) for s in (report.get("skipped") or [])],
            list(report.get("repair_notes") or []),
            bool(report.get("retry_attempted")),        # факт оркестратора
            int(report.get("retry_closed") or 0),
            str(report.get("package_reverted") or ""),
            dict(report.get("rules_regressed") or {}),
            int(report.get("noop_rows") or 0),
            int(report.get("truncated_operations") or 0),
            analysis or "",
            counter["calls"])
```

- [ ] **Step 4: replay-ветка** (`harness.py:561-577`) обязана повторять продуктовый путь, а не свою копию: `retry_attempted = bool(retry_ops) and (bool(skipped) or bool(unrouted_notes(notes)))` — и это единственное место, где replay что-либо выводит; всё остальное берётся из `apply_and_guarantee` (Task B1), иначе P0-4 не закрыт.
- [ ] **Step 5: набор метрик.** Удалить `improve/retry_needed_share`, `improve/applied_share`, `improve/skipped_share`, `improve/no_regression`. Взамен:

```python
    suite.metric("improve/op_acceptance", _op_acceptance,
                 description="доля операций пакета, оставшихся неприменёнными "
                             "после повтора (reapplied из знаменателя сняты)")
    suite.metric("improve/noop_share", lambda c: _share(c.noop_rows, c.applied),
                 direction=metrics.LOWER,
                 description="доля «применённых» строк, не изменивших схему")
    suite.metric("improve/retry_gain", _retry_gain,
                 description="доля отказов первого раунда, закрытых повтором")
    suite.metric("improve/package_revert_share", lambda c: float(bool(c.package_reverted)),
                 direction=metrics.LOWER,
                 description="доля пакетов, откачанных гарантом целиком")
    suite.metric("improve/rules_regressed_share", lambda c: float(bool(c.rules_regressed)),
                 direction=metrics.LOWER,
                 description="принятый пакет сломал хотя бы одно правило скоринга")
    suite.metric("improve/plan_truncated_share", lambda c: float((c.plan_truncated or 0) > 0),
                 direction=metrics.LOWER,
                 description="план обрезан лимитом операций: мерим не модель, а срез")
    suite.metric("improve/repeat_exact_share", _repeat_share, direction=metrics.LOWER,
                 description="повтор вернул дословно тот же отказ")
    suite.metric("improve/score_delta", ...)          # остаётся
    suite.metric("improve/pass@1", ...)               # остаётся, наследует генерацию
    suite.metric("improve/defects_repaired", ...)     # остаётся — честная
    suite.metric("improve/defects_introduced", _introduced, direction=metrics.LOWER,
                 description="инварианты, которые прошли до и упали после пакета")
```

`_op_acceptance`, `_retry_gain`, `_introduced`, `_share`, `_repeat_share` — модульные функции с `Optional`-возвратом (`None`, когда знаменателя нет). Значения в `LOWER_IS_BETTER` (`harness.py:40-43`) обновить синхронно: удалить `improve/skipped_share`, добавить четыре новых LOWER-метрики.

- [ ] **Step 6: тест на подмену.** Прогнать реальный оркестратор с двумя ответами (первый — отказ по пулу, второй — та же правка с валидным пулом) и assert'ом к харнесс-формуле: `retry_attempted is True`, `retry_closed == 1`, `op_acceptance is None`. Это тот самый случай, где старая метрика давала 0.0.
- [ ] **Step 7: Checkpoint.**

### Task A4. `dump_schemes` для улучшения писал только «после» (P2)

**Files:** `eval/harness.py:1093-1101`, `tests/test_eval_harness.py`

- [ ] **Step 1:** тест: после `dump_schemes` у каждого improve-кейса есть пара файлов `improve_<key>.before.bpmn` / `.after.bpmn` и `<key>.report.json` с `{applied, skipped, package_reverted, rules_regressed, score_before, score_after}`.
- [ ] **Step 2:** правка цикла — писать `base.xml` (он уже есть в `GenCase`, прокинуть `case.base_xml` из `run_improvement_case`) и отчёт рядом; в имя файла вернуть вердикт (`_verdict(case)` = `pass`/`revert`/`regress`), как у генерации.
- [ ] **Step 3:** `--dump-schemes DIR` глазами: две схемы рядом, diff по узлам/потокам/документации. **Step 4: Checkpoint.**

### Task A5. «КТО ПОРОДИЛ ДЕФЕКТЫ» смешивал два контура (P2)

**Files:** `eval/attribution.py:227-241`, `eval/harness.py:886-889`, `tests/test_eval_attribution.py`

- [ ] **Step 1:** тест: `tally()` вызывается раздельно по `report.cases` и `report.improve_cases`; в `render_table` два блока — «ГЕНЕРАЦИЯ: КТО ПОРОДИЛ» и «УЛУЧШЕНИЕ: КТО ПОРОДИЛ».
- [ ] **Step 2:** правка вывода; метка владельца отказа — `отказано аплайером:<stage>` вместо `аплайер:<stage>` (стадия по-английски, метка по-русски не стыковались: печаталось «аплайер:plan»), и `откачено гарантом:<причина>` для `stage="repair"`.
- [ ] **Step 3: Checkpoint.**

---

## WP-B. Аплайер: единый гарант, исполнимые операции

### Task B1. `apply_and_guarantee` + `merge_notes` (закрывает P0-2, P0-4, P1-5)

**Files:** `core/bpmn_edits.py` (после `apply_operations`, ~:2268), `tests/test_bpmn_edits.py`

- [ ] **Step 1: три теста, все красные.**

```python
def test_rejected_package_empties_applied(LINEAR_XML):
    """Откат по незащищённному циклу возвращает базу, значит ни одна правка
    в схеме не осталась: applied обязан быть пустым, а каждая строка — уехать
    в skipped с причиной отката."""
    ops = [{"op": "add_task", "id": "new_X", "name": "Шаг", "task_type": "userTask",
            "after": "A", "to": "B"},
           {"op": "connect", "source": "B", "target": "A"}]
    xml, rep = apply_and_guarantee(LINEAR_XML, ops,
                                   reject=lambda c: "цикл без выхода" if has_cycle(c) else None)
    assert xml == LINEAR_XML
    assert rep["applied"] == []
    assert rep["package_reverted"] if False else rep["reverted"] == "цикл без выхода"
    assert [s["reason"] for s in rep["skipped"]].count("цикл без выхода") == 2

def test_notes_survive_the_second_repair(LINEAR_XML):
    """Понижение одновыбросного шлюза до задачи — пометка неидемпотентная:
    второй прогон починки её не вернёт, и она обязана остаться в накопленных."""
    notes = merge_notes(["шлюз G понижен до задачи"], [])
    assert notes == ["шлюз G понижен до задачи"]

def test_reject_predicate_sees_the_repaired_xml(LINEAR_XML):
    seen = []
    apply_and_guarantee(LINEAR_XML, [], reject=lambda c: seen.append(c) or None)
    assert len(seen) == 1 and "<incoming>" in seen[0]   # после починки ссылок
```

- [ ] **Step 2:** FAIL (`apply_and_guarantee` не существует).
- [ ] **Step 3: реализация** (ровно та последовательность, что сейчас руками написана в `llm_improve.py:1003-1033`, плюс сверка отчёта):

```python
def merge_notes(*groups):
    out, seen = [], set()
    for group in groups:
        for note in group or ():
            text = str(note)
            if text not in seen:
                seen.add(text)
                out.append(text)
    return out


NOOP_NOTE_MARKERS = ("добавлено не было", "схема не изменилась")


def apply_and_guarantee(xml_text, operations, reject=None):
    root_report = ...  # см. ниже
    xml_after, report = apply_operations(xml_text, operations)
    notes = []
    for _round in range(2):                      # починка до состояния покоя
        xml_after, round_notes = validate_and_repair(xml_after)
        fresh = [n for n in round_notes if n not in notes]
        notes = merge_notes(notes, round_notes)
        report["repair_rounds"] = report.get("repair_rounds", 0) + 1
        if not fresh:
            break
    reverted = ""
    if reject is not None:
        reason = reject(xml_after)
        if reason:
            for entry in report["applied"]:
                rolled = {k: v for k, v in entry.items() if k != "note"}
                rolled["reason"], rolled["hint"] = reason, REJECT_HINT
                report["skipped"].append(rolled)
            report["applied"], xml_after, reverted, notes = [], xml_text, reason, []
    report["notes"] = notes
    report["reverted"] = reverted
    report["noop_rows"] = sum(1 for a in report["applied"]
                              if any(m in (a.get("note") or "") for m in NOOP_NOTE_MARKERS))
    report["status"] = ("failed" if reverted else
                        "success" if not [s for s in report["skipped"]
                                          if not s.get("reapplied")] else "partial")
    return xml_after, report
```

`REJECT_HINT = "перестроьте пакет так, чтобы он не ухудшал схему: " "сохраните маршрут и защищённый выход из цикла"` — константой, чтобы оркестратор не сочинял подсказку на месте.

Важно про `stranded`: откат односторонних новых узлов остаётся внутри `apply_operations` (он уже сверяет `applied`, `:2221-2233`), а повторная проверка после починки (`rollback_stranded`) вызывается из `reject`-предиката оркестратора (Task C1) — предикат видит и цикл, и stranded, и регресс правил, и возвращает причину.

- [ ] **Step 4: Checkpoint** `python -X utf8 -m pytest tests/test_bpmn_edits.py -q`.

### Task B2. Операция `add_condition` для существующего потока (P1-2)

**Files:** `core/bpmn_edits.py` (`OP_SPEC`, `_HANDLERS`, новый `_op_add_condition`), `tests/test_bpmn_edits.py`

Дефект, который она закрывает: скоринг требует условие на каждой ветке расходящегося шлюза (−15), а у уже существующей ноги единственный путь — `disconnect`+`connect` со сменой id потока. На `vehicle_reservation` это стоило всему пакету: применённая косметика при непройденном правиле.

- [ ] **Step 1: тест**

```python
def test_add_condition_names_the_flow_and_sets_the_leg(GATEWAY_XML):
    xml, rep = apply_operations(GATEWAY_XML, [
        {"op": "add_condition", "flow": "F3", "condition": "остаток есть"}])
    assert rep["status"] == "success", rep["skipped"]
    assert '<conditionExpression>остаток есть</conditionExpression>' in xml
    assert 'id="F3"' in xml                      # id потока сохранён


def test_add_condition_rejects_flow_into_a_task_and_names_the_op(GATEWAY_XML):
    _, rep = apply_operations(GATEWAY_XML, [
        {"op": "add_condition", "flow": "F7", "condition": "х"}])  # F7 из задачи
    assert rep["skipped"][0]["reason"].endswith("не является веткой шлюза")
```

- [ ] **Step 2:** FAIL («неизвестная операция»).
- [ ] **Step 3:** в `OP_SPEC`: `"add_condition": '{"op":"add_condition","flow":"id потока шлюза","condition":"текст условия"}'`; обработчик:

```python
def _op_add_condition(op: Dict[str, Any], index: _Index) -> List[str]:
    """Условие на уже существующей ноге шлюза. `connect` для этого не годится:
    он создаёт поток и на существующую пару отвечает «такой поток уже
    существует», — из-за чего требование скоринга «условие на каждой ветке»
    было неисполнимо (прогон vehicle_reservation: косметика при -15)."""
    flow_id = str(op.get("flow") or "")
    text = str(op.get("condition") or "").strip()
    if not text:
        raise _Skip("не задано условие", 'укажите condition: "текст"')
    flow = index.find_flow(flow_id)
    if flow is None:
        raise _Skip(f"поток '{flow_id}' не найден",
                    "id потока берётся из ИНВЕНТАРЯ раздела flows")
    if _local(flow.tag) != "sequenceFlow":
        raise _Skip(f"'{flow_id}' не sequence-поток",
                    "условие бывает только на ветке исключающего шлюза")
    gateway = index.elements.get(flow.get("sourceRef") or "")
    if gateway is None or _local(gateway.tag) not in GATEWAY_TAGS:
        raise _Skip(f"поток '{flow_id}' выходит не из шлюза",
                    "условие задают ноге шлюза; у шага выбора нет")
    if gateway is not None and _local(gateway.tag) != "exclusiveGateway":
        raise _Skip(f"шлюз '{gateway.get('id')}' не исключающий",
                    "condition читает только исключающий шлюз")
    notes = []
    if gateway.get("default") == flow_id:
        del gateway.attrib["default"]
        notes.append(f"выход по умолчанию со '{flow_id}' снят: теперь он условный")
    existing = flow.find(_q("conditionExpression"))
    if existing is not None:
        flow.remove(existing)
        notes.append("прежнее условие заменено")
    ET.SubElement(flow, _q("conditionExpression")).text = text
    return notes or [f"условие добавлено к '{flow_id}'"]
```

- [ ] **Step 4:** `_HANDLERS["add_condition"] = _op_add_condition`. **Step 5:** тест-замок от фантомов (закрывает P1-2 навсегда):

```python
def test_every_hint_names_only_known_operations():
    """Подсказка об операции, которой нет в OP_SPEC, отправляет модель
    повторять отказ: в `_require_element` годами жил «add_condition», которого
    аплайер не умел."""
    hints = " ".join(str(spec) for spec in OP_SPEC.values())
    for handler in _HANDLERS.values():
        ...  # собираем тексты hint'ов, вызывая отказы на заведомо битых op
    for name in re.findall(r"\b(add_\w+|remove_\w+|set_\w+|move_\w+|merge_\w+|disconnect|delete|rename|connect)\b", hints):
        assert name in OP_SPEC, f"упомянутая операция {name!r} не определена"
```

- [ ] **Step 6: Checkpoint.**

### Task B3. `add_event` без определения больше не создаёт пустой кружок (P0-3, локальная часть)

**Files:** `core/bpmn_edits.py:1205-1230`, `tests/test_bpmn_edits.py`

- [ ] **Step 1: тест**

```python
def test_catch_event_without_definition_is_refused_with_the_field_name(LINEAR_XML):
    """Промежуточное событие без *EventDefinition bpmn-js рисует пустым
    кружком, и принятое «улучшение» роняло скоринг на реальных eval-схемах
    (production_incident 95 -> 90) без единой пометки."""
    _, rep = apply_operations(LINEAR_XML, [
        {"op": "add_event", "id": "new_C", "name": "Ожидание ответа",
         "event_type": "intermediateCatch", "after": "A", "to": "B"}])
    assert rep["skipped"][0]["reason"] == "событию нужно определение"
    assert "event_definition" in rep["skipped"][0]["hint"]
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** в ветке `else:  # event`, сразу после вычисления `tag`/`definition`:

```python
        if definition is None and tag in ("intermediateCatchEvent",
                                          "intermediateThrowEvent"):
            raise _Skip(
                "событию нужно определение",
                "укажите event_definition=timer|message|error|signal "
                "(таймеру — duration или cycle): без определения bpmn-js "
                "рисует пустой кружок и скоринг не засчитывает тип события",
            )
```

- [ ] **Step 4: Checkpoint.** Внимание: фикстура `warehouse_delivery.live.improve.json` содержит ровно такую операцию в `retry_operations` — её отказ станет другим (`событию нужно определение` вместо `недостижим`). Фикстуру **не правим** (это запись живого ответа), но ожидаемое число skipped-строк в тестах харнесса может сдвинуться: проверять прогоном, а не подгоном порога.

---

## WP-C. Оркестратор: один путь, честный отчёт, донесённые находки

### Task C1. Перевод `improve_diagram` на `apply_and_guarantee` (P0-2, P0-4, P1-5)

**Files:** `core/llm_improve.py:859-1088`, `tests/test_improve_orchestrator.py`

- [ ] **Step 1: тест против фантомного промпта**

```python
def test_retry_never_tolds_reverted_edits_as_applied(LINEAR_XML_STUB, orchestrator,
                                                     monkeypatch):
    """Откатанный по циклу пакет возвращает базу, а пропт коррекции не должен
    утверждать «это уже применено — не повторяйте»: иначе модель обязана
    добивать правку, которой в инвентаре нет."""
    plan = json.dumps({"analysis": "а", "operations": [
        {"op": "add_task", "id": "new_X", "name": "Шаг", "task_type": "userTask",
         "after": "A", "to": "B"}, {"op": "connect", "source": "B", "target": "A"}]})
    fake = FakeLLM(monkeypatch, _wrap(plan), _wrap('{"analysis":"б","operations":[]}'))
    analysis, xml_after, report = _improve(orchestrator, LINEAR_XML_STUB)
    assert report["applied"] == []
    assert report["package_reverted"]
    assert "УЖЕ ПРИМЕНЕНО" not in fake.prompts[1]
    assert "не применено" in analysis.lower()
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3: тело.** Убрать локальные `_reject_cycle` и `_drop_stranded`, позвать один вход:

```python
        def _reject(candidate: str) -> Optional[str]:
            """Гаранты над уже отпочиненной схемой: структура, цикл, разбавление
            документации, регресс правил. Порядок фиксирован — от дешёвого."""
            created = {str(e.get("id")): str(e.get("op") or "")
                       for e in report_acc["applied"]
                       if e.get("id") and str(e.get("op") or "") in bpmn_edits.ADD_NODE_OPS}
            if created:
                candidate_after, stranded = bpmn_edits.rollback_stranded(candidate, created)
                if stranded:
                    for drop in stranded:
                        report_acc["applied"] = [
                            e for e in report_acc["applied"]
                            if drop["id"] not in (str(e.get("id") or ""),
                                                  str(e.get("source") or ""),
                                                  str(e.get("target") or ""))]
                        report_acc["skipped"].append({
                            "op": drop["op"], "id": drop["id"], "stage": "repair",
                            "reapplied": False,
                            "reason": f"новый шаг ({drop['id']}) {drop['gap']} — "
                                      "изменение откачено после починки",
                            "hint": drop["hint"]})
                    candidate = candidate_after
                    report_acc["notes"] = [n for n in report_acc["notes"]
                                           if drop["id"] not in n]
            if candidate == xml_content:
                return None
            if not has_unguarded_cycle(xml_content) and has_unguarded_cycle(candidate):
                return ("пакет создал цикл без защищённого выхода — изменение "
                        "откачено")
            regressed = _rules_regressed(xml_content, candidate)
            if regressed:
                return "пакет сломал правила скоринга: " + ", ".join(
                    f"{k} ({v})" for k, v in sorted(regressed.items()))
            return None

        xml_after, report = await asyncio.to_thread(
            bpmn_edits.apply_and_guarantee, xml_content, operations, _reject)
```

(В `apply_and_guarantee` мутация `report_acc` из замыкания допустима ровно потому, что она **только** собирает диагностику, а судьбу пакета решает один вызов; `applied` после `reject` она обнуляет сама.)

- [ ] **Step 4: новый гарант вместо трёх ручных** — `bpmn_scoring.diff_scores` уже существует (`:681`), переиспользуем:

```python
def _rules_regressed(base_xml: str, new_xml: str) -> Dict[str, str]:
    """Правила, которые проходили на базе и провалены после пакета. Guarantee
    «принятое улучшение не делает схему хуже» не мог опираться на балл: два
    сдвинутых правила дают ноль дельты при сломанном третьем."""
    try:
        delta = diff_scores(_scorer.evaluate(base_xml), _scorer.evaluate(new_xml))
    except Exception:  # noqa: BLE001 — скоринг не имеет права ронять улучшение
        return {}
    return {name: f"{v['before']}->{v['after']}"
            for name, v in (delta.get("rules") or {}).items()
            if v["before"] == PASSED and v["after"] == FAILED}
```

и экспорт `PASSED`/`FAILED`/`diff_scores` из `core.bpmn_scoring`.

- [ ] **Step 5: `notes` не затираются.** Оба места, где был второй `validate_and_repair`, сворачиваются в один `apply_and_guarantee` на раунд; `report["repair_notes"] = bpmn_edits.merge_notes(first["notes"], retry["notes"])`.
- [ ] **Step 6: факты в отчёт:**

```python
        report["retry_attempted"] = retried
        report["retry_closed"] = sum(1 for s in report["skipped"] if s.get("reapplied"))
        report["package_reverted"] = first_revert or second_revert or ""
        report["rules_regressed"] = _rules_regressed(xml_content, xml_after)
        report["noop_rows"] = report.get("noop_rows", 0)
```

- [ ] **Step 7: тексты `analysis`.** При `package_reverted` — не строка `план: batch: …`, а «Ни одно изменение не применилось: <причина>» (список `applied` пуст, и пользователь должен читать это, а не 5 строк отказов). При `rules_regressed` на отказе гаранта — перечислить правила и какую операцию скоринг советует (берётся из нового `action`, WP-D).
- [ ] **Step 8: Checkpoint** `python -X utf8 -m pytest tests/test_improve_orchestrator.py -q`.
- [ ] **Step 9: контракт API.** `app/routers/ai.py:105` отдаёт `report` наружу — поля только добавлены, ни одно не переименовано; проверить grep'ом по `bpmn-constructor/src/ImproveChat.js`, что читаемые ключи (`applied`, `skipped`, `status`, `truncated_operations`) на месте.

### Task C2. `_op_key` не закрывал `remove_participant`: продукт врал об успешной правке (P1-3)

**Files:** `core/llm_improve.py:694-698`, `tests/test_improve_orchestrator.py`

- [ ] **Step 1: тест** (тот же прогон, что в ревью: пул «Клиент» удалён, а отчёт спорит)

```python
def test_participant_keyed_op_gets_reapplied_credit(two_pool_xml, orchestrator,
                                                    monkeypatch):
    first = json.dumps({"analysis": "а", "operations": [
        {"op": "remove_participant", "participant": "Клиент"}]}, ensure_ascii=False)
    second = json.dumps({"analysis": "б", "operations": [
        {"op": "delete", "id": "cA"},
        {"op": "remove_participant", "participant": "Клиент"}]}, ensure_ascii=False)
    FakeLLM(monkeypatch, _wrap(first), _wrap(second))
    analysis, xml_after, report = _improve(orchestrator, two_pool_xml)
    assert "Клиент" not in xml_after
    assert [s["reapplied"] for s in report["skipped"]] == [True]
    assert "Не применено" not in analysis
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** переписать `_op_key` на общий словарь идентичности аплайера:

```python
def _op_key(entry: Dict[str, Any]) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    """Пара «операция — все поля идентичности». Ключ берётся из
    `bpmn_edits.op_identity_fields`, чтобы отказ, закрытый повтором той же
    правкой, узнавался по всем полям: на `remove_participant` старая версия
    читала только id/source/target и никогда не ставила `reapplied`, а
    пользователь читал «не применено» об уже удалённом пуле."""
    op = str(entry.get("op") or "")
    return op, tuple((f, str(entry[f])) for f in bpmn_edits.OP_IDENTITY_FIELDS
                     if entry.get(f))
```

в `bpmn_edits` экспортировать `OP_IDENTITY_FIELDS = _OP_IDENTITY_KEYS` (одним именем, два места — один источник) и `_op_identity` перевести на него.
- [ ] **Step 4: Checkpoint** + `pytest tests/test_bpmn_edits.py -q` (общий кортеж полей не должен сломать сверку applied/skipped внутри аплайера).

### Task C3. Находки скоринга: срез по весу, названные скрытые, доставлены в повтор (P1-1)

**Files:** `core/llm_improve.py:795-813, 665-677, 877-887, 1116-1130`, `tests/test_improve_orchestrator.py`

- [ ] **Step 1: тест**

```python
def test_findings_block_keeps_the_heaviest_and_names_the_cut(SCORING_HEAVY_XML):
    """Срез по позиции в словаре правил выбрасывал role_pools с готовым
    адресатом «Кладовщик → ВкусВилл» и guarded_cycles(-10), оставляя naming(-10)
    и no_isolated(-8): модель не видела дефекта, о котором её учили говорить."""
    block = _findings_block(SCORING_HEAVY_XML)
    shown = [ln for ln in block.splitlines() if ln.startswith("—")]
    assert len(shown) <= MAX_SCORING_FINDINGS + 1
    hidden = [r for r in _scorer.evaluate(SCORING_HEAVY_XML)["recommendations"]
              if not any(r.split(":")[0][:24] in ln for ln in shown)]
    heaviest_shown = min(_rule_weight_of_line(ln) for ln in shown)
    if hidden:
        assert heaviest_shown >= max(_weight(r) for r in hidden)
    assert "скрыто:" in block and ", ".join(sorted(map(_rule_name, hidden))) in block
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** `_scorer.evaluate()` возвращает `details_meta[правило]["weight"]` — сортировка по нему, имена скрытых правил в конце блока:

```python
def _findings_block(bpmn_xml: str) -> str:
    """УЗКИЕ МЕСТА ПО СКОРИНГУ: за что с этой схемы уже сняли баллы.

    Порядок — по весу правила, а не по порядку объявления: срезанные
    «последние» оказывались role_pools и guarded_cycles, то есть самые
    переживаемые бизнес-дефекты. Скрытые правила называются по имени: «и ещё
    N» не говорит модели, чего она не видит, а молчаливый срез превращает
    план в отредактированный."""
    evaluation = _scorer.evaluate(bpmn_xml)
    ranked = sorted(evaluation["recommendations"],
                    key=lambda r: -_weight_of_recommendation(evaluation, r))
    lines = "".join(f"— {r}\n" for r in ranked[:MAX_SCORING_FINDINGS])
    hidden = [name for name, meta in sorted(evaluation["details_meta"].items(),
                                            key=lambda kv: -kv[1]["weight"])
              if meta["status"] == FAILED
              and not any(name_line == name for ...)]  # см. примечание
    if hidden:
        lines += "— скрыто лимитом (чините после первых): " + ", ".join(hidden) + "\n"
    ...
```

Реализация `hidden` — через `details_meta`, а не матчинг текста: `_findings_block` должен собирать блок **по правилам**, а не по `recommendations` (иначе имя правила придётся выводить из строки). То есть: `for name in правила с FAILED, сортировка по весу: строка = message + note + elements + " → чинится: " + action`. Это же даёт точный `hidden`.
- [ ] **Step 4:** `MAX_SCORING_FINDINGS` поднять до 10 — теперь это верх по весу, а не произвольные первые. В `notes` промпта зафиксировать, что лимит транспортный.
- [ ] **Step 5:** блок узких мест добавляется в `_RETRY_TEMPLATE` (`{findings_block}`) и пересчитывается на **промежуточной** схеме: переспрос обязан видеть и то, что уже починилось, и то, что осталось. В `llm_improve.py:1118` передать `findings_block=_findings_block(intermediate_xml)`.
- [ ] **Step 6: тест доставки** — `assert "скрыто лимитом" in fake.prompts[1] or "УЗКИЕ МЕСТА" in fake.prompts[1]`.
- [ ] **Step 7: Checkpoint.**

---

## WP-D. Скоринг: исполнимое действие на каждое правило + бизнес-слой

### Task D1. Поле `action` у 17 правил и проверка «только известная операция»

**Files:** `core/bpmn_scoring.py:593-611, 646-664`, `tests/test_scoring.py`

- [ ] **Step 1: тест-замок**

```python
def test_every_failed_recommendation_names_an_executable_operation(BROKEN_ALL_XML):
    """Правило, совет которого не выражается пакетом операций, — жалоба без
    работы: модель тратит корректирующий повтор на формулировку. Проверка
    имена операций берёт из OP_SPEC, а не из пересказа."""
    from core import bpmn_edits
    ev = BPMNScorer().evaluate(BROKEN_ALL_XML)
    assert len(ev["recommendations"]) >= 10
    for rec in ev["recommendations"]:
        ops = set(re.findall(r"\b[a-z_]+(?=[ ,.)]|$)", rec)) & set(bpmn_edits.OP_SPEC)
        assert ops, f"рекомендация без исполнимой операции: {rec[:60]}"
```

- [ ] **Step 2:** FAIL на `naming`, `documentation`, `end_event`, `task_types`, …
- [ ] **Step 3:** `'action'` в `self.rules`: `start_event`/`end_event` → `add_event`; `pool_has_steps` → `add_task`; `participant_interacts` → `connect` (`flow_type=message`); `gateway_conditions` → `add_condition`, `set_default`; `gateway_split_join` → `add_gateway`, `connect`; `sequence_flows`/`no_isolated` → `connect`, `after`; `naming` → `rename`; `guarded_cycles` → `connect`, `add_condition`; `element_count` → `delete`, `merge_participants`; `boundary_events` → `add_boundary_event`; `task_types` → `add_task` (`task_type`); `pool_lanes` → `move_to_lane`, `add_lane`; `role_pools` → `merge_participants` (уже есть в тексте); `event_types` → `add_event` (`event_definition`); `documentation` → `add_documentation`. Строка рекомендации: `message (: note)? (элементы: …) → чинится: rename, add_documentation`.
- [ ] **Step 4: Checkpoint.**

### Task D2. Пять бизнес-правил о логике процесса (главная часть «максимальной эффективности»)

**Files:** `core/bpmn_scoring.py` (новые `_check_*` + `rules`/`_checks`), `tests/test_scoring.py`

Каждое правило: детерминировано из структуры, называет получателя, лечится существующей операцией, не смотрит в текст на русском (иначе — словарь вместо признака и утечка в eval).

| правило | вес | признак (структурный) | получатель в тексте | action |
|---|---|---|---|---|
| `rework_loop` | 8 | цикл, в котором ≥1 активность и ни одна исходящая дуга цикла не подписана `conditionExpression` и не помечена `default` | id шагов цикла | `add_condition`, `set_default` |
| `handoff_pingpong` | 8 | две пула обмениваются messageFlow в обе стороны | «Склад ↔ Перевозчик» | `merge_participants` |
| `approval_chain` | 6 | ≥4 последовательных userTask в одной дорожке без шлюза между ними | имя дорожки + id шагов | `add_lane`, `move_to_lane`, `add_gateway` |
| `lane_overload` | 6 | ≥3 дорожки и >60% активностей процесса в одной | имя дорожки, доля | `move_to_lane`, `add_lane` |
| `wait_without_sla` | 8 | `intermediateCatchEvent` без `timerEventDefinition`, при том что в процессе есть хотя бы один таймер (то есть SLA измеряется, но не везде) | id события | `add_boundary_event`, `add_event` |

- [ ] **Step 1: тесты на признаках, а не на тексте** — по одному на правило: красная схема на 12 строках XML + зелёный аналог + `not_applicable` («нечего проверять» ≠ «прошли», иначе новый вес молча съедает знаменатель `score`).

```python
def test_rework_loop_without_a_signed_exit_names_its_steps(LOOP_XML):
    ev = BPMNScorer().evaluate(LOOP_XML)
    bad = [r for r in ev["recommendations"] if r.startswith(BPMNScorer().rules["rework_loop"]["message"][:20])]
    assert bad and "A4" in bad[0] and "add_condition" in bad[0]

def test_guarded_rework_is_not_a_bottleneck(GUARDED_LOOP_XML):
    assert BPMNScorer().evaluate(GUARDED_LOOP_XML)["details"]["rework_loop"] is True

def test_lane_overload_names_the_lane_and_the_share(OVERLOAD_XML):
    note = BPMNScorer().evaluate(OVERLOAD_XML)["details_meta"]["lane_overload"]
    assert note["status"] == "failed"
    msg = next(r for r in BPMNScorer().evaluate(OVERLOAD_XML)["recommendations"]
               if "Собрка" in r or "дорожк" in r)
    assert "% шагов" in msg and "move_to_lane" in msg
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** реализовать `_check_rework_loop` и компанию на готовом словаре `_Schema` (`preceding`, `outgoing`, `node_by_id`, `owner_of_node`, `lane_names_by_process`) — `_find_cycles`/`_is_guarded` уже есть, переиспользовать; `lane_overload` — по `flowNodeRef` (в `_Schema` добавить обратную ссылку `lane_of_node` одним проходом).
- [ ] **Step 4: знаменатель.** Суммарный вес вырастет с 146 до 182; `score` нормирован по применимым правилам, поэтому старые схемы не «упадут» из-за объёма — но **упадут из-за новых провалов**. Проверить на всем eval-корпусе и **не** ослаблять правила ради цифр: вместо этого зафиксировать новый baseline и читать diff по правилам (`diff_scores`).
- [ ] **Step 5: Checkpoint** `python -X utf8 -m pytest tests/test_scoring.py -q`.

### Task D3. `_findings_block`: бизнес-узкие места первее косметики

**Files:** `core/llm_improve.py:798-813, 812`

- [x] **Step 1:** блок разделён на секции (`_FINDING_SECTIONS`): `ТОЧКИ УЛУЧШЕНИЯ
  ПРОЦЕССА` (бизнес-правила), `НАРУШЕНИЯ НОТАЦИИ BPMN`, `ОФОРМЛЕНИЕ (процесс не
  меняет)` — косметика идёт последней. Разделение по `BUSINESS_RULES`/
  `COSMETIC_RULES` из `bpmn_scoring`, чтобы оркестратор не пересказывал словарь
  скоринга; пустая секция не печатается.
- [x] **Step 2: тест** — порядок секций закреплён (`test_findings_are_sectioned_and_the_cut_names_what_it_hid`):
  бизнес-правило с весом 6 стоит выше `naming` с весом 10, а заголовок не может
  ни появиться без своей находки, ни опереться на выдуманное. Это и есть
  исправление «мера не та, что заявлена»: `naming` больше не вытесняет узкое
  место.
- [ ] **Step 3: Checkpoint.**

---

## WP-E. Утечки и независимость оракула

### Task E1. `provenance` не смотрел на improve-фикстуры (подтверждено инъекцией)

**Files:** `eval/provenance.py:251-273`, `tests/test_eval_provenance.py`

- [ ] **Step 1: тест-инъекция** — фикстура `kind:"improve"`, чьи операции скопированы из few-shot образца промпта улучшения, обязана дать находку:

```python
def test_improve_fixture_copied_from_the_prompt_example_is_a_leak(tmp_path):
    evil = {"id": "x.improve", "kind": "improve", "scenario": "warehouse_delivery",
            "operations": provenance.examples()[1]["payload"]["operations"]}
    findings = provenance.fixture_findings([evil], provenance.examples())
    assert any(f["example"] == "improve_package" for f in findings), \
        "харнесс слеп к пакету, повторяющему образец промпта"
```

- [ ] **Step 2:** FAIL (сегодня `fixture_findings` обходит только `plan`, и инъекция проходила pure green: «пересечений нет» при 5/5 совпавших имён операций).
- [ ] **Step 3:** в `fixture_findings` — вторая ветка по `kind == "improve"` против образца `improve_package` (имена операций + `analysis`), порог `NAME_OVERLAP`. Для improve-фикстур пород ниже (0.4): пакет из 12 операций копируется легче, чем план из 20 элементов.
- [ ] **Step 4:** заодно — `scenario_findings` обязан сверять `scenario.text` и со статическими кусками промпта улучшения (правила 1-12, OP_SPEC), а не только с `improve_prompt`.

### Task E2. Статические тексты, которые модель читает целиком: правила скоринга и OP_SPEC

**Files:** `eval/provenance.py:104-183`, `tests/test_eval_provenance.py`

- [ ] **Step 1:** новый вход `examples()`: `{"id": "scoring_rules", "where": "core.bpmn_scoring.BPMNScorer.rules", "text": <все message+action+note>, "names": set(), "rules_only": True}` и `{"id": "op_spec", "where": "core.bpmn_edits.OP_SPEC", ...}`.
- [ ] **Step 2:** `scenario_findings` для `rules_only`-образцов: `mentions(participant, text)` → находка «требование скопировать имя пула». Именно так в генерации ловили «WMS/перевозчик/клиент» в правилах.
- [ ] **Step 3: проверить и почистить текущие тексты.** Запустить `python -X utf8 -m eval.provenance`; при находке по имени из `expected_participants` — **менять текст правила/примера, а не порог**. Пример: в `OP_SPEC` и в подсказках аплайера встречаются «Склад», «ВкусВилл», «перевозчик» (например `_pool_skip` печатает имена **схемы** — это данные, не промпт, и под проверку не попадает).
- [ ] **Step 4: тест-замок «новый промпт с образцом = новый вход»:**
```python
def test_every_prompt_that_hands_the_model_an_example_is_audited():
    from core import bpmn_edits, bpmn_generator, llm_improve
    audited = " ".join(e["text"] for e in provenance.examples())
    for blob in (str(llm_improve._SYSTEM_PROMPT), str(bpmn_generator._SYSTEM_PROMPT),
                 "\n".join(bpmn_edits.OP_SPEC.values()),
                 "\n".join(r["message"] for r in BPMNScorer().rules.values())):
        for name in provenance.words(blob) & ALL_EVAL_PARTICIPANT_NAMES:
            assert name in audited, f"имя {name!r} отдано модели вне аудита"
```

### Task E3. Оракул обязан мерить бизнес-слой независимо от скоринга

**Files:** `eval/invariants.py:56-76, 371-590, 675-691`, `tests/test_eval_harness.py`

Без этого `improve/score` и «найдено узкое место» — самоотчёт: правка веса в `bpmn_scoring.py` передвигала бы и линейку, и измеряемое значение. Ограничения модуля (только `xml.etree`, `parse_xml`, локальные словари типов) сохраняются.

- [ ] **Step 1: новые инварианты** в `CORE_INVARIANTS`: `no_blind_rework` (цикл без подписанного выхода), `pools_not_pingpong`, `no_overloaded_lane` (порог >0.6 при ≥3 дорожках), `waits_have_sla`. Реализация — отдельным проходом по `_Graph`, **не** импортируя `bpmn_scoring` (тест-замок на запрет импорта уже нужен: добавить в `tests/test_eval_harness.py` assert по исходнику модуля).
- [ ] **Step 2: сверка знаков, не значений.** Тест, который на 4 подготовленных XML требует: `BPMNScorer().details[name] is False` ⇔ `invariants.check_xml(...)[invariant].ok is False`, где соответствие задаётся словарём `SCORING_TO_ORACLE = {"rework_loop": "no_blind_rework", ...}`. Это ловит разошедшиеся реализации до того, как метрика начнёт «улучшаться».
- [ ] **Step 3:** `not_applicable` обязателен для всех четырёх (схема без дорожек ≠ перегруженная дорожка).
- [ ] **Step 4: Checkpoint.**

### Task E4. Quality-разметка фикстур: авторский пакет ≠ живой ответ модели

**Files:** `eval/fixtures/*.json`, `eval/scenarios.py`, `eval/harness.py:177-204, 671-689`, `tests/test_eval_harness.py`

Репозиторий уже различает «эталон» и «живой» ответ (`quality`), но improve-метрика их смешивает: `production_incident.good.improve` написан руками и гарантированно даёт `applied_share 1.0`. Это надует любую долю.

- [ ] **Step 1:** поля `quality: "real" | "authored"` сделать обязательными для `kind == "improve"` (сейчас `authored` стоит в `provenance`-описании, но в метрике не участвует); загрузчик падает, если поля нет.
- [ ] **Step 2:** в `render_table` и в `coverage_note` — разбивка `improve/*` по качеству, плюс метрика `improve/authored_share` (доля выборок, написанных руками): читатель видит, что n=1 real не есть вывод о контуре.
- [ ] **Step 3:** **не** домножать выборку синтетикой ради `defects_repaired`. Живых прогонов сегодня нет (`GIGACHAT_CREDENTIALS` отсутствует) — значит `defects_repaired` остаётся с n=1, и это надо читать как «данных нет», а не как «0». Поэтому для replay-пути добавляется `improve/repair_coverage`: доля инвариантов, которые хотя бы в одном реальном кейсе меняли значение после пакета (покрываемость метрики, а не качество модели) — она растёт от структуры харнесса, а не от подгона ответов.

---

## WP-F. Сведение: максимум честных чисел

### Task F1. Полный прогон и разбор глазами

- [ ] `python -X utf8 -m pytest tests/ -q` → все зелёные; число тестов обновить в `AGENTS.md` (753 → фактическое).
- [ ] `python -X utf8 -m eval.run --mode replay --dump-schemes reports/improve-fixes` → 5 пар «до/после» глазами: что стало лучше, что косметика, что сломано.
- [ ] Повторить 6 проб ревью (`%TEMP%\qoder-probe\probe*.py`) — каждый обязан now показать исправленное поведение: applied пуст при откате; `event_definitions` не падает от принятого пакета; `remove_participant` закрыт повтором; `role_pools` в блоке находок; пометка о понижении шлюза живёт при 2 вызовах модели.
- [ ] `python -X utf8 -m eval.provenance` → «находок: 0» при включённых новых проверках.
- [ ] `--fail-on-regression` на свежем baseline → код 0, и отдельно убедиться, что подделанная регрессия (`--threshold 0.001`) её ловит.

### Task F2. Документация

- [ ] `AGENTS.md`: 17 → фактическое число правил; «16 взвешенных правил» в `bpmn_scoring.py:1` → актуальное; описание новых метрик `improve/*` и почему старые выведены; строка про `apply_and_guarantee` как единую точку «пакет применён и не ухудшил схему».
- [ ] `.agents/guidelines/testing.md` или `architecture.md`: правило «новое правило скоринга обязано иметь `action` и независимый инвариант в оракуле» — иначе контур снова начнёт мерить себя.
- [ ] `docs/plans/improve-contour-effectiveness.md`: этот файл + отметки выполненных задач.

---

## Что сознательно НЕ делается

- Ослабление оракула, фикстур, инвариантов и порогов ради долей прохождения — красная линия ревью, и она же причина, по которой `defects_repaired` остаётся с n=1.
- Полномасштабный рефакторинг `BPMNImprovementOrchestrator`: планирование пакета остаётся на месте. Выносится только последовательность гарантов, потому что именно на её дублировании в двух местах (оркестратор ×2, харнесс ×1) выросли P0-2/P0-4/P1-5.
- Разделение транспорта и семантики в аплайере: транспорта в `bpmn_edits.py` нет, все обработчики — чистые правки дерева.
- Правки в `bpmn-constructor/` и `core/llm_client.py` (чужие зоны этой недели).
- Подмена живого прогона «реплеем с выдуманным анализом»: метрики, требующие текста `analysis`, в replay возвращают `None`.

---

## WP-G. RAG-шаг как измеряемая величина (найден и поставлен в план этим проходом)

Ни одна метрика харнесса не отвечала на вопрос «тот ли эталон подобрал поиск»,
хотя блок «ЛУЧШИЕ ПРАКТИКИ» уходит в промпт планирования на каждом вызове.
Добавлен `eval/retrieval.py` (P@k, R@k, NDCG@k, доля пустых ответов) с
релевантностью по домену эталона: корпус англоязычный и 62% его — `approval`,
поэтому пересечение слов с русским описанием нулевое при любом качестве
подбора, а домен — единственный признак, переживающий языковой барьер.
Разметка `RELEVANT_DOMAINS` живёт в eval и в промпт не попадает.

Замерено первым проходом (`python -m eval.retrieval`, 2026-09-25, семантическая
ветка `paraphrase-multilingual-MiniLM-L12-v2`, корпус 367 эталонов): оба
improvement-кейса — P@3 0.00. Контур приносил модели чужие по теме практики, и
до этого шага никто не утверждал, что RAG вообще что-то подбирает.

Причина нашлась в классификаторе домена: словарь ключевых слов был собран из
слов самого процесса («проверка», «подпись», «качество»), а не из отраслевых
существительных, поэтому склад уходил в `approval` (62% корпуса) на любом
вопросе. Правка — двуязычный отраслевой словарь + правило запаса
(`DOMAIN_MARGIN = 1.5`: без уверенного перевеса домен объявляется `general`),
кэш индекса перестроен (`CACHE_METHOD = hybrid-v4`).

Попутно классификатор закрыл и утечку: блок «ЛУЧШИЕ ПРАКТИКИ» печатал тег домена
эталона, и для `employee_onboarding` в промпт попадал `[hr]`, а оракул требует
пул с именем «HR» — модель могла вернуть имя участника из подсказки. Тег из
блока снят (заперт тестом «в блоке нет ни одного `[...]`»), аудит происхождения
теперь даёт пустой список находок.

Второй замер, после правки классификатора и расширения набора кейсов (у
метрики два источника: `improve` — записанный запрос, `etalon` — текст задачи
плюс эталонная схема того же сценария; двух improvement-фикстур на наборе мало,
чтобы отличить подбор от везения):

| кейс | P@3 | MRR@3 | релевантных в корпусе | домены выдачи |
|---|---|---|---|---|
| warehouse_delivery `[improve]` | 1.00 | 1.00 | 0.17 | logistics |
| production_incident `[improve]` | 0.00 | 0.00 | 0.46 | finance, logistics |
| loan_application `[etalon]` | 1.00 | 1.00 | 0.23 | finance |
| support_ticket `[etalon]` | 0.67 | 0.50 | 0.65 | customer_service, finance |
| purchase_approval `[etalon]` | 0.33 | 1.00 | 0.23 | finance, general |
| product_return `[etalon]` | 0.33 | 1.00 | 0.36 | customer_service, finance, general |

Свод: `etalon` n=4 — P@3 0.58 / MRR@3 0.88, `improve` n=2 — P@3 0.50 / MRR@3 0.50.
Мотивированная замена заголовка: `recall@k` при 367 эталонах и k=3 заперт у нуля
даже при идеальном подборе (знаменатель — десятки файлов) и для свода не годится;
вместо него в заголовке MRR@k, который измеряет различимое на таком k — насколько
высоко встал первый релевантный. Recall остался в разборе кейса, где видно
`relevant_in_corpus`.

Остаётся один провал — `production_incident`. Проверенная и снятая гипотеза:
дописать текст запроса в лексическую ветку (сейчас там только структура схемы и
домен). Замер дал те же числа до и после: в просьбах контуру нет различающих
слов, «найди узкие места» есть в описаниях почти всех эталонов и весит по IDF
почти ноль. Причина промаха в другом — домен берётся из схемы пользователя, а в
процессе инцидента участвует платёжный шлюз, и `finance` уводит выдачу в
платёжные процессы. Это чинится не ранжированием:

- [ ] Жёсткий фильтр по домену до ранжирования либо смешивание с мягким
      штрафом: если классификатор не уверен (нет запаса), искать по `general`
      и не пускать отраслевые документы в топ. Метрика готова показать эффект —
      она и есть критерий.
      Прежде чем брать эту задачу, надо решить вопрос, на который она отвечает,
      иначе результат прочтётся как улучшение там, где изменилась разметка:
      пул `{домен, general}` вернёт `general`-практики обратно (на них и держится
      `production_incident`), но снимет `warehouse_delivery` с 1.00 — его разметка
      считает релевантным только `logistics`. Либо разметка обязана признать,
      что `general` применим к любой отрасли, и тогда `P@3` меряет «не подсунул
      чужую отрасль», а не «нашёл иглу», и `relevant_share` из отчёта становится
      обязательным спутником каждого числа. Менять одно, не тронув другое,
      нельзя: это ровно та подмена меры, из-за которой план и заводит отдельную
      таблицу дебита эталона.
- [ ] Развести язык индекса: `element_names` вместе с русским переводом
      назначения, чтобы лексическая ветка не жила на одном англо-словаре.
- [ ] Если подбор на `production_incident` так и не станет тематическим,
      снятие блока честнее его присутствия: практика чужого домена в промпте —
      example pollution, и `provenance` это уже умеет показывать.
- [ ] Метрику завести в live-прогон: replay не поднимает индекс дешевле, чем
      сам контур, а цифры выше получены на живой семантической ветке.

## Ход этого прохода (что легло в код)

- Пункт «гарант по скорингу» из Task C1 реализован уже, чем планировалось, и
  это зафиксировано здесь, а не в коде: отказ пакета по дельте правил скоринга
  оказался слишком грубым инструментом. `passed→not_applicable` — не ухудшение
  (слей роли снимает `participant_interacts` с проверки), а `start_event` ломался
  на легальном слиянии. Отказ оставлен там, где он механически исполним: цикл без
  защищённого выхода и откатанное починкой узел вне маршрута (оба — внутри
  `bpmn_edits.apply_and_guarantee`). Регресс по правилам теперь факт: он топит
  переспрос, попадает в отчёт (`rules_regressed`) и читается пользователем в
  анализе; `improve/rules_regressed_share` меряет его по всему набору.
- Противоречие двух правил скоринга (`role_pools` велит сливать, `start_event`
  наказывает за слитое) решено в пользу семантики нотации: `start_event` теперь
  зеркало `end_event` — «у каждого участника свой старт», а не «число стартов
  равно числу участников». Пул без старта по-прежнему наказывается.
- Отказ наружу (`ImprovementError`) донесён до подсказки, а не только причины:
  `_refusal_text` схлопывает дословные повторы и прикладывает `hint`
  («верните ветку через исключающий шлюз…»), иначе отказ читается как диагноз
  без лечения.
- Подмена типа элемента починкой («шлюз понижен до задачи») показана
  пользователю: `REPAIR_OWN_NOTE_MARKERS` + блок в анализе. До этого по отчёту
  её видно не было.
- Промпт переспроса остаётся узким (находки скоринга в него не уезжают — это
  контракт, зафиксированный тестом), но получает блок сломанных правил.

## Ход второго прохода: измеримости бизнес-слоя отдаён отдельный гейт

Бизнес-инварианты оракула (`no_blind_rework`, `pools_not_pingpong`,
`no_overloaded_lane`, `waits_have_sla`) вошли в `CORE_INVARIANTS`, а `scenario_pass`
— ядро `pass@1/scenario` — конъюнкция по всем применимым инвариантам. На эталонных
планах это дало провал: кредитная заявка держит возврат той же работы в `A3`
(ответ Бюро приходит тому же шагу, который её запросил), складская — `receiveTask`
«зафиксировать подпись получателя» без единого таймера в модели. Проверено глазами
по фикстурам, не по догадке.

Значит `pass@1/scenario` по этому составу не выполним ни для какой схемы: потолок
метрики ниже единицы по построению, и модель, скопировавшая эталон точь-в-точь,
получила бы ноль. Это тот случай, который план называет «метрика противоречива» —
чинится метрика, а не оракул:

- Оракул расслоён: `NOTATION_INVARIANTS` (корректность: нарушение = «схему нельзя
  принять», эталон им удовлетворяет) и `BUSINESS_INVARIANTS` (узкое место
  процесса: конечная схема держит его всегда). `deciding_checks` берёт в гейт
  только первые, `applicable_checks` не изменилась.
- Ничего не перестало считаться: `business/smells` (плотность узких мест),
  `business/not_worse_than_etalon` (сравнение с эталоном того же сценария),
  `improve/business_repaired_share` (что из бизнес-дефектов базы пакет убрал) и
  `business_agreement` (где два слоя не сошлись). Дебит эталона заперт таблицей
  `EXPECTED_ETALON_BUSINESS_DEBT` с обоснованием по строке, а не молчанием.
- Усиление, а не ослабление: ровно те же провалы оракул находил и раньше, и
  тест «эталон не проваливает корректностных инвариантов» стал строже по
  содержанию (он теперь про слой, а не про «пустое множество вообще»).

Скоринг продукта по ожиданию без срока был свернут с оракулом, а не наоборот.
Три его слепых зоны закрыты, потому что каждая стоила ложного отказа или ложного
молчания: правило не знало `receiveTask` (этап подписи для него не существовал),
не засчитывало граничный таймер на самом ожидании и при этом советовало ровно
`add_boundary_event` — предложенная правка не снимала нарушение, а контур
улучшения получал регресс за собственную подсказку; и процесс без единого таймера
объявлялся «не применимым» — то есть правило молчало ровно на тех схемах, где
висящее ожидание опаснее всего. Расхождением осталось только то, что расхождение
и должно мерить: параллельную ветку «срок вышел» оракул принимает, скоринг нет.
(Снято в следующем разделе: обе линейки теперь знают одну форму.)

## Ход третьего прохода: подсказка должна быть исполнимой, а две линейки — сходящейся

Метрика `business/scorer_oracle_agreement` родилась из проверки, а не из
пожелания: оракул и скоринг независимо читают одни и те же бизнес-свойства, и
их расхождение — это дефект одного из прочтений. Проводка харнесса была неверна
с самого начала: `business_agreement` получал вырезку `details_meta` вместо
ответа, `_scorer_view` в плоской ветке читал `{правило: словарь}` как истину и
объявлял `passed` на каждом правиле. Таблица расхождений заполнялась
`oracle_stricter` на заведомо битой схеме, то есть метрика мерила баг проводки.
После починки (харнесс отдаёт весь ответ скоринга, `tests/test_eval_harness.py`
держит на это два теста) настоящий набор расхождений оказался равен одному
правилу, и каждое закрыто по факту, а не ослаблением:

- `lane_overload` против `no_overloaded_lane`: у скоринга была калитка «дорожек
  хотя бы три», из-за неё дежурная смена production_incident (4 работы из 5 на
  «Дежурном инженере», 80%) была для продукта «не применима», тогда как оракул
  её ловил. Скоринг проверяет теперь и двухдорожечные пулы, с порогом 75%
  (`LANE_MONOPOLY_SHARE`): перевес 2:1 остаётся разделением «делает / ждёт».
- `wait_without_sla` против `waits_have_sla`: расхождение по параллельной ветке
  «срок вышел» закрыто с обеих сторон. Скоринг признал три формы срока (таймер в
  самом ожидании, граничный таймер на активности, развилка с прямой ногой
  таймера), оракул ужесточился до той же формы — развилка обязана быть предком
  ожидания, таймер её прямой ногой, `exclusiveGateway` не считается. Ослабления
  не было ни одного: оракул перестал пропускать таймер «где-то в соседней ветке»,
  скоринг перестал советовать правку, которую аплайер отвергает.

Исполнимость подсказок (`TestAdviceIsExecutable` в `tests/test_scoring.py`) —
отдельный класс ошибок, который метрики не показывали, пока не стали считать
пакет из id, названных в тексте. Статическая проверка «`action` есть в `OP_SPEC`» её не
ловит: операция существует, но модель нечем заполнить. Что оказалось не исполнимо
на самом деле:

- `wait_without_sla` для catch-события советовал `add_boundary_event`, а аплайер
  на него отвечает «не является задачей». Легальный пакет — развилка:
  `add_event(timer)` без `after`, `add_gateway(parallel, after=предок)` без `to`,
  и два `connect` (шлюз→таймер, таймер→продолжение). Порядок шагов в подсказке
  не декоративный: аплайер вставляет новый узел посреди существующей дуги,
  поэтому нога-ветка собирается только `connect` после вставки шлюза.
- `handoff_pingpong` называл дуги обмена, но не следующий шаг того пула:
  `connect` было нечем заполнить, и модель уходила в `disconnect`, теряющий
  ответ. Теперь в тексте есть id приёмника, `disconnect(flow=...)` и
  `flow_type='message'`, а для обмена «пул целиком» — честный отказ советовать
  перевеску (`connect` участником не работает, «источник или цель не найдены»).
- `lane_overload` называл только дорожку-виновника. `move_to_lane` требует ещё и
  приёмник: подсказка перечисляет работы перегруженной дорожки и две
  дорожки-приёмника по загрузке, id и имя.

Потолок на рецепты (`SLA_HINTS_IN_MESSAGE = 3`): строка «УЗКИЕ МЕСТА ПО
СКОРИНГУ» уходит в промпт целиком и их число ограничено, поэтому пять одинаковых
развилок вытеснили бы другие правила. Экономия честная — нарушители остаются в
`elements` все до одного, а «и ещё N» говорит, сколько именно (тест
`test_many_unbounded_waits_get_one_recipe_not_five`).

Метрики этого прохода: `business/scorer_oracle_agreement` = 1.0 по всем 11
схемам replay-набора (было 0.886 на неверной проводке), `improve/base_advice_drift`
= 0 на обоих improve-кейсах. Доля расхождений перестала быть декоративной:
`--fail-on-regression` после перезаписи baseline проходит, `score` 89.091,
`pass@1/scenario` 0.545 — без изменений, потому что ни один оракул и ни одна
фикстура не ослаблены (число проваленных бизнес-инвариантов то же).

Мелочи, из-за которых прогон врал сам себе:
- `_check_handoff_pingpong` был переписан на новый признак с `edges` как `set()`,
  а использовался как `dict` → `AttributeError` из-за одного объявления
  расходился на 61 падающий тест, включая сверку оракула.
- `python -m eval.run` падал на windows-консоли с `UnicodeEncodeError` после того,
  как всё было посчитано и записано: `✗` нет в cp1251. Вывод теперь в UTF-8 с
  заменой символа, потому что «код возврата есть, таблицы метрик человек не
  видел» — это не результат прогона.
- `tests/test_source_hygiene.py`: кириллические комментарии правились несколько
  раз за сессию с вкраплениями идеограмм. Проверка по `tokenize` (только
  комментарии и строки, только идеограммы и признаки битой перекодировки)
  стоит на месте; проверка «латинское слово в русском тексте» мерить не
  удалось — 2253 вхождения, из них подавляющее большинство — имена полей и
  операций, то есть whitelist съел бы полезность теста.

Что осталось непроверенным, и это надо прочитать как границу результата:
live-контур не гонялся — `GIGACHAT_CREDENTIALS` в этом окружении нет, и харнесс
на это честно отвечает кодом 2. Все числа выше получены в replay на записанных
ответах модели. Поэтому два вывода план не закрывают: «переспрос с подсказкой
про `add_boundary_event` теперь приводит модель к таймеру» (в replay лежит
старый ответ модели, и `improve/business_repaired_share = 0` мерит именно его) и
«подбор практик перестал быть чужим на живых формулировках». Нужен прогон
`python -m eval.run --mode live` + `python -m eval.retrieval` с артефактами в
`reports/`, прежде чем эти два утверждения можно будет писать в отчёте.

## Ход четвёртого прохода: RAG-метрика меряла саму себя, а нарушение — не починялся

Начало прохода — единственное оставшееся нулевое число свода: `production_incident`
P@3 0.00 в `python -m eval.retrieval`. Разбор показал, что ноль принадлежал не
подбору, а разметке.

**Круговая разметка релевантности.** Поле `domain` у эталона заполняет
`BPMNKnowledgeBase._detect_domain` (`core/llm_improve.py:381`) — тот же
классификатор, что размечает запрос пользователя. Пока метрика сверяла выдачу с
ним же, она измеряла согласие системы с собой: промах подбора был неотличим от
промаха классификатора, а успех — от того, что обе стороны повторили одну ошибку.
Докладывал это место в `eval/retrieval.py` фразой «домен размечен в корпусе
руками», и она была неверна: рядом с `.bpmn` нет ни одного файла меток.

**Играбельность.** `general` у классификатора — 167 файлов из 367, и почти все
это демо элементов нотации (`timer_5`, `signal_3`, `gateways_2` — по 15 файлов на
каждый) и заготовки упражнений (`New_Process`, `Ex_3`). У двух сценариев
релевантным был указан именно `general`, то есть точность там росла от возврата
любого демо-файла. Плюс модуль сам себе противоречил: docstring утверждал, что
`general` из списков исключён намеренно, а в двух списках он стоял.

**Что легло в код.** `eval/corpus_topics.py` — ручная таблица тем по ИМЕНИ ФАЙЛА
корпуса (имена в датасете человеческие: `Dispatch_of_Goods_<uuid>`,
`Exercise_5_-_Credit_Scoring`), размечена человеком и от вывода контура не
зависит. Покрытие — 363 файла из 367, четыре неопознанных печатаются в шапке
отчёта, чтобы разрастание датасета новым семейством было видно, а не обнулило
метрику. `RELEVANT_DOMAINS` → `RELEVANT_TOPICS`, `relevant_domains()` →
`relevant_topics()`.

**Размер корпуса как факт.** Темы, которой в датасете нет, записаны в
`ABSENT_TOPICS` явно: закупка, ИТ-инцидент, найм, бронь транспорта. Значит
`production_incident` из замера выпадает как пробел корпуса, и ноль там был
правильным ответом метрики, но не про поиск, а про то, что корпус — набор
учебных примеров BPMN-курса, а не реестр бизнес-процессов ВкусВилл. Измеряемых
сценария осталось четыре.

**Второе число, которого не было.** `practice_coverage` — доля конструкций,
которые сценарий заявляет (`must_have_timer` → таймер, `must_branch` →
параллель), показанных хотя бы в одном подобранном этлоне. Оно нужно не как
украшение: в промпт из RAG попадают только строки практик, домен эталона туда не
печатается сознательно (тег `[hr]` ловился провенансом как утечка — оракул
требовал пул с этим именем). То есть продукт из шага получает именно практики, а
тема — диагностика.

**Замеры (один и тот же харнесс, `--k 3`, менялась только лексическая сигнатура).**

| вариант | P@3 | MRR@3 | NDCG@3 | practice |
|---|---|---|---|---|
| `hybrid-v4`: точные счётчики, домен без ограничения | 0.50 | 0.58 | 0.81 | 0.75 |
| полосы `few_tasks`/`many_tasks` вместо чисел | 0.33 | 0.25 | 0.69 | — |
| `hybrid-v5`: числа + признаки практик, домен по корпусу | 0.42 | 0.38 | 0.67 | 0.92 |

Гипотеза «сток формы» (точные счётчики ранжируют по совпадению количеств, и
кредитная заявка получает top-3 из шаблонов упражнений) подтвердилась как эффект и
не подтвердилась как польза: полосы его убирали и роняли единственный работавший
кейс `warehouse_delivery` с P@3 1.00 до 0.00. Размер процесса — тот признак,
который в датасете на 42 заготовки отличает содержательную схему от пустой. Числа
оставлены, память об этом держит `TestStructuralSignature::test_counts_stay_exact`.

Взвешивание веток RRF проверено и отклонено: (1,1) → 0.42/0.38/0.92, (2,1) →
0.33/0.46/0.58, (3,1) → 0.25/0.38/0.42, (1,0) — только семантика → 0.25/0.25/0.42
при NDCG 1.00 (это NDCG одного релевантного на первом месте при знаменателе из
трёх, не качество). Равные веса остаются.

Ограничение домена корпусом (`_detect_domain(xml, present)`) на сводке нейтрально
— замер в одном процессе с включённым и выключенным ограничением дал идентичные
0.42/0.38/0.92: кейсы с темой `it` в сводку не входят. Оно осталось, потому что
утверждение про тему, под которую в датасете ноль документов, — это шум в запросе,
а не сигнал.

**Нарушение, у которого не было починки.** `event_types` у скоринга и
`event_definitions` у оракула ругались на пустой кружок `intermediateCatchEvent`,
а применимой правки не существовало: `add_event` создаёт НОВОЕ событие, и стоящий
узел с его потоками оставался нарушением. В аплайере — восемнадцатая операция
`add_event_definition` (`id` события + `event_definition` + `duration`/`cycle` для
таймера); второе определение — отказ, а не «применено», иначе рядом с таймером
появлялось message и событие ждало бы и то и другое. Рецепт в подсказку
`event_types` собран из id нарушения и проверяется `TestAdviceIsExecutable`.

Ловушка, которую эта операция открыла, закрыта текстом, а не ослаблением правила:
`add_event_definition(event_definition='timer')` внутри ожидания снимает нарушение
`wait_without_sla` формально, потому что событие, которое будит только таймер,
перестает ждать сообщения, — то есть починка удаляла бизнес-ожидание, и оракул
её бы не поймал.
Рецепт срока называет граничный таймер на активности или ногу развилки; запрет
записан в docstring обеих сторон (`_deadline_hint`, `_op_add_event_definition`).

**Итог прохода.** 989 тестов зелёные, `python -m eval.run --mode replay
--fail-on-regression` — exit 0, провенанс чистый. RAG: P@3 0.42 / MRR@3 0.38 на
четырёх измеримых сценариях, practice_coverage 0.92, четыре сценария выведены
отдельной строкой как пробел датасета. Из метрик ушёл единственный играбельный
ноль, и вместе с ним — иллюзия, что подбор эталонов на этом корпусе измерен:
сводка теперь говорит «тема подобрана» только там, где теме есть с чем
сравнивать.

**Синоним имени операции вместо отказа.** Разбор `improve/op_acceptance` показал,
что две правки из одиннадцати в записанном пакете умерли формулировкой
«неизвестная операция» при верных операндах: модель звала соединение `add_flow`.
Слово взялось не из воздуха — `sequenceFlow` есть в словаре BPMN, а в подсказках
починки «добавь поток» встречается чаще, чем `connect`. В аплайере `OP_ALIASES`
(пока одна пара: `add_flow` → `connect`): подстановка не даёт новых возможностей,
она перенаправляет в существующий обработчик с теми же полями, и в отчёт ложится
заметка «имя операции 'add_flow' заменено каноническим 'connect'», чтобы принятое
не приписывало модели каноническое имя, которого она не давала. Guard-тест
`test_no_hint_or_spec_invents_an_operation` теперь считает именем легальным и
ключ `OP_ALIASES`.

Числа: `improve/op_acceptance` 0.733 → 0.800, в кейсе схемы изменили 9/15 вместо
7/15, `improve/score_delta` 0.0 → 0.5 (контур впервые не просто «применил пакет»,
а поднял балл схемы), baseline перезаписан. Оставшиеся отказы трогать не стали
осознанно: «такой поток уже существует» — настоящий no-op, «не задано имя
элемента» и «событию нужно определение» — настоящие дыры в ответе модели, а
ослабление этих требований подняло бы ровно тот показатель, который оно и должно
ловить.

## Ход пятого прохода: линейку сверяли с реальными схемами, а не с фикстурами

Фикстур в харнессе одиннадцать, и они построены вокруг того, что правила уже
умеют находить, — слепоту правила на них поймать нельзя. Перепись `python -m
eval.coverage` по 367 рукописным `.bpmn` показала три попадания подряд:
`naming` снимал балл за 15380 узлов, из которых задачами были 18 (имя события
или шлюза ноты не требуют и `rename` к ним не приложим), `documentation` считал
долю по всем элементам, а правку просил у шагов (18552 нарушителя, 1828 из них —
задачи), `approval_chain` на всём корпусе не сработал ни разу: он считал только
`userTask` внутри названной дорожки, а подписи в импортированных схемах стоят
как `task` и без дорожек. После калибровки правило находит 37 схем с четырьмя
ручными шагами подряд, и `eval/coverage.py` пинит это тестом
`MUST_FIRE_ON_CORPUS` — иначе перепись пришлось бы повторять руками.

Четвёртое попадание было ложным: `wait_without_sla` ругался на 2731 узел, и 675
из них — `linkEventDefinition`, метка перехода, которую токен не ждёт, а
`compensateEventDefinition` — внутренний триггер отработки. Требовать у них срок
значит советовать таймер на узле, где он ничего не означает; оракул
(`_blocking_waits`) сужен тем же жестом, независимо и с тем же списком
определений, чтобы расхождение двух прочтений не стало самоцелью.

**Второй замер того же модуля — сверка двух прочтений на всём корпусе**
(`agreement_census`). Скоринг и оракул читают один XML независимо, так что это
единственная проверка линейки, которой не нужна человеческая разметка «где
правильно». Числа: за весь корпус — ни одного «простили» и ни одного «не
смотрели»; расходятся только `lane_overload` (18 схем, пороги 60% против 75% —
задокументированный замысел) и `rework_loop` (8 схем: оракул спрашивает
различимость ног развилки, скоринг — охрану дуг, отпускающих цикл). Классы
расхождения разложены по `CLASS_OF_PAIR`, потому что вердикт «оракул строже»
сам по себе не отличает прощённый дефект от другого знаменателя, а тест
`unexplained(agreement) == []` не даёт этому различию стёрться.

Перепись поймала и собственную ошибку: `business_agreement` ждёт ответ `evaluate`
целиком, а в перепись сначала передавался `details_meta` — он не проходит ни по
одному из разбираемых форматов, и каждый статус читался как «passed», из-за чего
сверка рапортовала бы, что линейка простила все нарушения сразу. Держит это
отдельным тестом на одной схеме, которую оба слоя зовут нарушением.

**Отказ, мотивированный замером.** Правило неразрешённых ссылок
(`sourceRef`/`targetRef`/`processRef`/`flowNodeRef`/`incoming`/`outgoing`/
`default`/`attachedToRef`) не добавлено: промер по тем же 367 файлам дал ноль
нарушений во всех классах ссылок, то есть правило было бы вечно слепым, а
`coverage` честнее бы не стало. Также не добавлено и засчитывание подписи дуги в
`rework_loop` (оракул её читает): у восьми расходящихся схем дуга, отпускающая
цикл, безымянная (метки «Yes»/«No» стоят на развилке выше), поэтому смягчение
уронило бы требование, не закрыв ни одного расхождения.

**Аплайер: у отказа «id без new_» появился адрес.** `add_event` с id готового
события — это правка, а не создание, и прежняя подсказка предлагала модели
«придумать id с new_», отчего в схеме появлялся второй пустой кружок, а
`event_types` оставался нарушен. `_new_id_hint` для пустого промежуточного
события теперь называет `add_event_definition(id='…', event_definition=…)` —
исполнимую по этому же id правку; на задаче и на событии с определением
подсказка остаётся прежней.

**Итог прохода.** 1012 тестов зелёные, `python -m eval.run --mode replay
--fail-on-regression` — exit 0. Метрики харнесса не двинулись (pass@1/scenario
0.545, score 89.09, business/smells 0.455, not_worse_than_etalon 0.889,
scorer_oracle_agreement 1.0): фикстуры набора не содержат ни меток перехода, ни
цепочек согласований без дорожек, а нарушителями `naming` и `documentation` в
сгенерированных схемах и так оказывались шаги. Это честный результат прохода,
который правил точность детекции, а не долю прохождения, и он же — напоминание,
что корпус eval и корпус датасета измеряют разные вещи: первый — контур,
второй — линейку.

## Ход шестого прохода: подсказку проверял аплайер, а два слоя — друг друга на всём корпусе

Пятый проход сверял, *что* правила находят на корпусе. Шестой отвечает на два
вопроса, на которые фикстуры отвечать не могли: исполнима ли найденная подсказка
и не прощает ли один слой то, что видит другой.

### `eval/advice.py`: исполнимость подсказки, измеримая аплайером

Совет уходит в промпт улучшения дословно, и текст, который нельзя выразить
пакетом операций, контур читает как «придумай id»: аплайер отвечает отказом, а
`improve/op_acceptance` записывает это качеством модели, хотя виноват текст
правила. Перепись собирает пакет ИСКЛЮЧИТЕЛЬНО из операндов, названных в
подсказке, и прогоняет его через продуктовый гарант на реальных схемах. Разбор
строгий: операнд — только `ключ='значение'`, поэтому `connect(source=этот шлюз)`
даёт операцию без обязательного поля ровно так, как её увидит аплайер.

Находки первой же переписи (12 схем на правило):

| правило | что было | что стало |
| --- | --- | --- |
| `wait_without_sla` | рецепт звал `add_boundary_event` на catch-событие (аплайер отвергает «не является задачей»), а нога развилки писалась словами | таймер, развилка и обе дуги названы id; у каждого ожидания свой `new_sla_timer_N` — прежний фиксированный id делал второй и третий рецепт неприменимым |
| `handoff_pingpong` | «ответ — следующему шагу» без имени шага | `disconnect(flow=…)` + `connect(source=…, target=…)` по уже существующим id; на пуловом конце и на «нечему отвечать» — причина словами |
| `lane_overload` | «перенеси в другую дорожку» без работ и без приёмника | `move_to_lane(id=…, lane=…)` на те работы, что снимают перегруз, и список дорожек-приёмников |
| `approval_chain` | совет без операндов | `add_gateway(after=…, to=…)` внутри дорожки, `add_lane` + `move_to_lane` в процессе без дорожек |
| `rework_loop` | `add_condition` на дугу, которая ногой параллельной развилки | условие предлагается только там, где оно выразимо; иначе «правка не выражается операцией» |
| `event_types` | альтернатива `timer\|message\|error\|signal` на catch-событии | `timer` из вариантов снят: таймерное определение внутри ожидания убирает нарушение ценой самого ожидания |

Итог по 12 схемам на правило: `применено == названо` у всех шести, снятие
нарушения — 92% у `handoff_pingpong` и `lane_overload`, 64–67% у
`wait_without_sla` и `approval_chain`, 50% у `event_types`. Остаток — не брак
текста, а потолок `RECIPES_IN_MESSAGE`: совет называет три рецепта из N,
остальное доходит следующим кругом. Чтобы это было видно, перепись ходит по
кругам сама (`rounds_to_clear`): `lane_overload` при повторе подсказки доезжает
до 100%. Классификация «рецепта нет» расслоена на три числа — `вне операций`,
`операнд выбирает модель` и `МОЛЧИТ О ПРИЧИНЕ`; последнее обязано быть нулём, и
тест это держит.

### Висячий поток: дефект, который прощали оба слоя, и ложная вина подсказки

Перепись показала две «регрессии по вине совета» (`no_isolated` и
`sequence_flows` после `disconnect` + `connect`). Разбор по адресу схемы
оказался другим: в исходной схеме дуга `sequenceFlow` не имела `targetRef`
вовсе. `validate_and_repair` такие дуги вырезает, и тупик появлялся *после*
правки, а скоринг до правки его не видел — дуга без конца засчитывалась узлу за
выход. Замер корпуса: 107 таких дуг в 33 схемах из 367 (9%), и в 15 из них
`sequence_flows` проходил целиком. Оракул прощал то же: его `_Graph` заводил
смежность по неразрешённому концу.

Правка в обоих слоях, независимо: в `_Schema` и в `_Graph` дуга попадает в
граф, только если оба её конца — узлы схемы; повисшие концы собраны в
`dangling_flows` / `unresolved`, и `sequence_flows` теперь называет их id с
половиной рецепта (`disconnect(flow=…)`) и честной причиной для второй половины
(цель знает только автор процесса). Ложных «регрессий» в переписи не осталось
(0 у всех шести правил), а регрессия `_Schema`-порядка построила и второй
замер: смежность теперь собирается после прохода дерева, потому что поток в XML
встречается раньше узла, к которому ведёт.

### Нотационный слой сверен тем же механизмом, что и бизнес-слой

`scorer_oracle_agreement` сверял четыре бизнес-правила, и слепая зона
нотации в него не попадала вовсе. `business_agreement` получил параметр
`pairs`, `eval/coverage.py` — таблицу `NOTATION_PAIRS` и вторую секцию
отчёта. Расхождения оказались настоящими и односторонними:

- `participant_interacts` — 35 схем, где оракул объявлял пул немым, хотя
  messageFlow концом висел на самом участнике (BPMN 2.0 это разрешает, Signavio
  так рисует «фронт» банка). Инвариант стоит в гейте `pass@1/scenario`, то есть
  гейт штрафовал корректные схемы; скоринг в это время был прав.
- `no_isolated` ↔ `no_unrouted` — те же 2 схемы с висячим потоком, описанные
  выше.

После правок 7 из 8 нотационных пар сходятся на всех 367 схемах, а
`gateway_split_join` расходится на 6: оракул требует схождения любой развилки,
скоринг смотрит эксклюзивные и на чужих шлюзах объявляет себя неприменимым. Это
записано в `DOCUMENTED_DIVERGENCES` и осталось очередью работы скоринга, а не
«разночтением, которое договорились не чинить».

### Свои ошибки измерения этого прохода

- `_CALL_RE` разбирал тело вызова до первой скобки: `participant='Scoring
  (Bank)'` ронял всю операцию из разбора, и перепись показывала «connect: цель не
  найдена» там, где в пакете просто не было `add_event`. Тело допускает один
  уровень вложенных скобок, случай закреплён тестом.
- Отказ в отчёте подписывался именем *первой* схемы правила, а не той, где он
  случился: разбор «pinpoint-дефекта» уходил не в тот файл. Теперь имя берётся
  из той же позиции списка.
- `wait_without_sla` на ожидании без дуг советовал «развилку до этого ожидания»
  без причины. Теперь говорит, что вставить ногу некуда и сначала связность,
  и перепись считает это предельным, а не промахом текста.

### Чего сознательно не делали

- Не подставляли в `connect` выдуманные значения вида `target='следующий шаг'`:
  аплайер отвергает несуществующий id, а перепись показала бы исполнимый рецепт
  там, где его нет.
- Не предлагали `merge_participants` автоматом для пинг-понга «нечему отвечать»:
  слить два пула — решение автора процесса, а не структурный вывод.
- Не трогали гейт `pass@1/waits_have_sla = 0`: это долг эталонного ответа
  (`EXPECTED_ETALON_BUSINESS_DEBT`), а не дефект контура.

### Итог прохода

Тесты: 1031 (было 1014). `python -m eval.run --mode replay --fail-on-regression`
— без регрессий, метрики прогона не изменились (`pass@1/scenario` 0.545,
средний балл 89.09, `business/smells` 0.455,
`business/not_worse_than_etalon` 0.889, `scorer_oracle_agreement` 1.0): правки
этого прохода меняли детекцию на рукописном корпусе и тексты подсказок, а не
ответы подставной модели. Очередь: сходимость не-эксклюзивных развилок в
скоринге, `RECIPES_IN_MESSAGE` как единый знаменатель трёх правил, живой прогон
исполнимости подсказок (нужен `GIGACHAT_CREDENTIALS`).

## Ход седьмого прохода: нотационный слой сверялся до конца, а исполнимость мерилась по всем правилам

### Последняя пара нотационного слоя разошлась по-настоящему

`gateway_split_join` — единственная пара, остававшаяся в расхождении (6 схем
корпуса). Разбор по witness-файлам показал, что это не разные пороги, а разное
прочтение нотации, и ошибались оба слоя, только в разные стороны:

- скоринг не смотрел на `inclusiveGateway`. По подходящим веткам такого шлюза
  идёт несколько токенов, и неразведённая нога — тот же висящий маршрут.
  Замер: 10 схем корпуса с разветвлённым inclusive, схождение есть у всех, то
  есть правка ничего не добавила к нарушениям — но правило перестало их
  пропускать. Добавлен в `SPLIT_GATEWAY_TAGS`.
- оракул требовал схождения у `eventBasedGateway`. Его ноги ждут разных
  событий, срабатывает одна, и схождения потоков по семантике нет; инвариант
  входит в гейт `pass@1/scenario`, а на корпусе таких ложных срабатываний было 7
  из 98 схем с разветвлённым event-шлюзом. Исключён в `_splits`.

После этого таблица `NOTATION_PAIRS` сходится на всех 367 схемах: 8 пар, 0
расхождений, `DOCUMENTED_DIVERGENCES` в нотационном слое пуст. Решение о том, у
какой развилки обязано быть схождение, закреплено тестом
`test_event_based_fork_is_not_a_split_while_inclusive_fork_is` — за ним стоит
один кортеж тегов в каждом слое, и без теста он разъехался бы молча.

### Исполнимость подсказки по всем 22 правилам: 14 молчат

`python -m eval.advice --all-rules` (по 6 схем на правило) — то же измерение,
развёрнутое на все правила, чей совет уходит в промпт улучшения. Бизнес-слой
после шестого прохода чист (`названо == применено`, снятие 83–100%), а
нотационный и косметический — нет: 14 правил не называют ни операции, ни
причины. Их «→ чинится: rename, connect» — имя операции без операндов, и модель
заполняет их выдуманными id.

| правило | вес | нарушитель (операнд, который правилу уже известен) | что должно быть в подсказке | что остаётся модели |
| --- | --- | --- | --- | --- |
| `gateway_conditions` | 15 | шлюз + id ног без условия | `add_condition(flow='…')` по каждой названной ноге | текст условия |
| `start_event` / `end_event` | 10 | участник без старта/финиша (49 и 69 элементов на корпусе) | `add_event(id='new_start_N', event_type='start', participant='…')` + `connect` к первому шагу пула | имя события |
| `pool_has_steps` | 10 | пустой участник | `add_task(id='new_task_N', participant='…')` + `connect` | имя шага |
| `sequence_flows` / `no_isolated` | 10 / 8 | дуга и её источник; узел без входа или выхода | `connect(source='…')` с уже названной стороной | вторая сторона |
| `guarded_cycles` | 10 | id дуг, отпускающих цикл | `add_condition(flow='…')` | текст условия |
| `naming` / `documentation` | 10 / 7 | шаги без имени или описания | `rename(id='…')` / `add_documentation(id='…')` | текст |
| `task_types` | 8 | задачи без типа | операция из его же `action` по названному id | тип шага |
| `pool_lanes` | 8 | пул без дорожек при ролях | `add_lane(id='new_lane_N', participant='…')` | имена дорожек |
| `element_count` | 8 | — (схема целиком) | назвать кандидатов на удаление по списку | выбор |
| `participant_interacts` | 8 | немой участник | межпуловой конец аплайер не принимает, поэтому — конкретный шаг этого пула | выбор контрагента |
| `gateway_split_join` | 10 | шлюз + незакрытые ноги | `add_gateway(gateway_type='parallel', after='…', to='…')` для схода | узел схода |

Порядок работ: `gateway_conditions` (вес 15, операнды уже в `elements`), затем
`start_event`/`end_event`/`pool_has_steps` (вес 10, нарушитель — участник, а
значит `participant=` в рецепте берётся из его же id), затем связность
(`sequence_flows`, `no_isolated`, `guarded_cycles`). Косметические правила
(имя, документация, тип шага) — последними: их операнд по определению пишет
автор, и перепись должна отличать «назван id, текст за моделью» от молчания.

Чтобы очередь не росла незаметно, в `tests/test_eval_advice.py` заморожен
список `SILENT_ADVICE_RULES` и висит проверка `applied == named` по всем
правилам: молчаливое правило может только выбыть из списка, попасть в него
новое — падения теста.

### Половинчатый рецепт `sequence_flows` убран

Тот же замер показал на `sequence_flows` 3 схемы из 6, где подсказку применили
буквально и правило `no_isolated` упало: совет звал `disconnect(flow=…)` по
висячей дуге, а удаление дуги без её замены и есть тупик. Теперь правило
называет id дуги и её источника и прямо говорит, что цель выводится только из
смысла процесса; тест держит и это (`disconnect(flow=` в тексте быть не должно),
и то, что полная правка (`connect` + `disconnect`) нарушение снимает без
регрессий.

### Итог прохода

Тесты: 1033. `python -m eval.coverage` — обе сверки (бизнес и нотация) чисты
кроме двух объявленных пороговых расхождений. `python -m eval.advice
--all-rules` — названное применяется по всем правилам, 14 правил в очереди на
операнды. Метрики прогона не изменились (`pass@1/scenario` 0.545, балл 89.09,
`scorer_oracle_agreement` 1.0): правки прохода меняли прочтение рукописного
корпуса, а не ответы подставной модели; `pass@1/waits_have_sla = 0` остаётся
долгом эталонного ответа.

## Ход восьмого прохода: исполнимость мерилась по всем 22 правилам, молчание сокращено с 14 до 5

### Перепись, развёрнутая на весь скор

`python -m eval.advice --all-rules` — тот же замер исполнимости, что закрыл
бизнес-слой, но по всем правилам: в блок «УЗКИЕ МЕСТА ПО СКОРИНГУ» уходит до 10
находок, и косметические правила толкаются там же, где бизнес-узкие места.
Первый прогон (по 6 схем на правило) дал 14 правил, чей совет не называет ни
операции, ни причины, при том что бизнес-слой был уже чист.

Закрыто в этом проходе (подсказка теперь говорит id там, где они выводимы из
схемы, и словами — где их вывести нельзя):

| правило | что называет подсказка сейчас |
| --- | --- |
| `start_event`, `end_event` | `add_event(id='new_start_N', event_type='start', name='Начало', participant='…')` + `connect` к первому (последнему) шагу маршрута пула; адрес пула берётся тем же путём, что и в рецепте срока, — по узлу, а не по имени участника |
| `pool_has_steps` | `add_task(id='new_step_N', participant='…', after='старт', to='финиш')`, когда в пуле есть дуга старт→финиш; когда её нет — сказано, что вставлять не во что |
| `participant_interacts` | `connect(source='шаг говорящего пула', target='первый шаг немого', flow_type='message')` по существующим id |
| `no_isolated`, `sequence_flows` | id узла и недостающая сторона (вход/выход) + причина, по которой второй конец не выводится структурно |
| `gateway_conditions` | id ног без условия и прямо сказанное «текст условия называет автор процесса» |
| `gateway_split_join` | id незакрытых ног и причину, по которой узел схождения не выводится |
| `naming` | id безымянных шагов; `rename` остаётся без текста, и это тоже сказано |

Классификация «рецепта нет» теперь трёхсоставная и печатается в отчёте:
`вне операций` (правка не выражается операцией), `операнд выбирает модель`
(структура известна, значение пишет автор) и `МОЛЧИТ О ПРИЧИНЕ` (дефект
текста). Последний счётчик сведён к 5 правилам — `documentation`,
`element_count`, `guarded_cycles`, `pool_lanes`, `task_types` — и зафиксирован
в тесте как потолок: `SILENT_ADVICE_RULES` может только сокращаться. Инвариант
`применено == названо` держится по всем 22 правилам: перепись сама поймала две
мои ошибки, пока я их не успел задокументировать, — шаблонный `rename(id='…')`
в тексте `naming` (разбирался как вызов без обязательного значения и
показывал «названо, но неприменено») и `add_event` без соседа в пустом пуле
(аплайер отвечает «пул не определён», теперь там нет вызова вовсе).

### Что осталось в очереди после переписи

- `naming` упал на одной схеме не из-за подсказки: `validate_and_repair`
  понижает шлюз с единственной веткой до задачи, а у шлюза имени по нотации не
  бывает — нарушитель `Warenversand_9d83f8d992b647feb794f8441e98cb56.bpmn`,
  `sid-678AFF9B…`. Гарант порождает безымянную активность; честное решение —
  либо вырезать такой шлюз сращиванием маршрута, а не понижать, либо не
  понижать безымянный вовсе (выбрано второе, см. «Ход девятого прохода»).
- `pool_has_steps`: на 6 схемах корпуса дуги старт→финиш нет (пустой пул без
  событий вовсе), поэтому рецепт остаётся словами. Нужен разбор «куда вставлять
  шаг в пустом маршруте» — это же и лечит пункт выше.
- `documentation`, `task_types`, `pool_lanes`, `element_count`,
  `guarded_cycles` — текстовые или выбор-операнды: требуется тот же разбор,
  что уже сделан для `naming` (назвать id + сказать, что значение пишет автор).
- Потолок `RECIPES_IN_MESSAGE = 3` остаётся единственным знаменателем долей
  «снято с одного пакета»; круги починки (`rounds_to_clear`) показывают, что
  `lane_overload` доезжает до 100%, а `wait_without_sla` и `event_types`
  упираются в число нарушений, а не в текст.

### Итог прохода

Тесты: 1035. `python -m eval.advice --all-rules --per-rule 6` — `применено ==
названо` по всем правилам, 5 молчаливых в очереди, `сломано другое правило` —
1 случай, и он про аплайер, а не про текст. `python -m eval.coverage` —
нотационная таблица сходится на 367 схемах целиком. Прогон без регрессий:
`pass@1/scenario` 0.545, балл 89.09, `scorer_oracle_agreement` 1.0 — метрики не
двинулись, потому что менялись тексты подсказок и прочтение рукописного
корпуса, а не ответы подставной модели.

---

## Ход девятого прохода: заплатка не должна сама плодить нарушения

**Что нашлось.** Перепись исполнимости на `sequence_flows` дала два случая
«совет сломал другое правило», и оба оказались не советом, а гарантом:
`validate_and_repair` понижает шлюз с единственной веткой до задачи, у шлюза же
имени по нотации нет → на свет появляется безымянная активность и падает
`naming`. Свидетель — `Warenversand_9d83f8d992b647feb794f8441e98cb56.bpmn`,
`sid-678AFF9B…`. Тот же путь проходил генератор
(`_demote_single_branch_gateways`), и там он заполнял имя латиницей (`task`), то
есть прятал симптом, а не причину.

**Решение.** Понижать можно только шлюз, у которого имя уже есть или которое
выводится из подписи ветки; безымянный остаётся шлюзом с заметкой гаранта
(«имени нет, а понижение до задачи требует имени»). Правка зеркалирована в двух
слоях — аплайер и генератор, — иначе «чинится» только путь улучшения, а
генерация по-прежнему выдаёт безымянную задачу.

**Закрытые молчавшие правила.** Очередь из пяти («`guarded_cycles`,
`task_types`, `documentation`, `element_count`, `pool_lanes`») разобрана той же
формой, что и `naming`: подсказка называет id из схемы и открыто говорит, что
значение пишет автор процесса («тип шага называет автор», «и ещё N тем же»).
После этого `SILENT_ADVICE_RULES` опустел до `frozenset()`, а тест
`test_no_rule_advises_without_operands_or_reason` с пустым списком — это уже не
«наследие, которое нельзя раздвинуть», а нулевая терпимость: молчаливое правило
не может появиться незаметно.

**Итог.** 1036 тестов, `--all-rules`: названо == применено, сломано — 0, молчат
— [].

## Ход десятого прохода: два невидимых класса границ потока

**Как искали.** Перепись корпуса и сверка двух слоёв дают меньше, чем перебор
классов, которых в корпусе нет, но которые запрещены нотой. Поэтому проходом
раньше корпус был обходит поперёк правил: для каждой дуги спрашивали не «есть ли
её конец», а «в каком процессе лежат её концы» и «что вообще может быть концом
потока сообщения».

**Найденное (корпус, 367 рукописных схем).**

| класс | дуг | схем | что прощалось |
|---|---|---|---|
| `sequenceFlow` между двумя пулами | 8 | 5 | дуга выглядела маршрутом: `sequence_flows` проверял существование конца, `no_isolated` — его наличие, `handoff_pingpong` не спорил, потому что обмен между пулами выглядит как обмен |
| `messageFlow` без конца или на несуществующий id | 8 | 6 | `participant_interacts` считает, кого обмен затронул; сломанный конец ни кого не затрагивает, и схема выглядела «мало участников» |

Запрет при этом давно жил в контуре как транспорт: `connect` с
`flow_type=sequence` между процессами аплайер отвергает со словами «используйте
flow_type=message», а `validate_and_repair` (шаг 1b) межпуловую дугу либо
переделывает в сообщение, либо вырезает. Линейка не видела ровно того, что
гарант умел чинить, — то есть пользователь, загрузивший такую схему, получал
чистый скоринг над сломанным процессом.

**Что добавлено.**

- `core/bpmn_scoring.py`: `_Schema.dangling_messages`, правила
  `cross_pool_flow` (вес 10) и `message_flow_ends` (вес 8) — линейка выросла с
  22 правил и веса 190 до 24 и 208.
- `eval/invariants.py`: независимые `flows_within_pool` и `message_flow_ends`
  (оракул читает принадлежность по **имени** пула, скоринг — по id процесса:
  разные читательские пути, как с висячими дугами), включены в
  `NOTATION_INVARIANTS`, то есть в гейт приёмки.
- `eval/coverage.py`: пары в `NOTATION_PAIRS` + оба правила в
  `MUST_FIRE_ON_CORPUS`; сверка на корпусе — 10 пар, расхождений 0.

**Рецепт и его цена.** `cross_pool_flow` — первое правило про дугу, совет которого
исполним целиком: `disconnect(flow=…) + connect(source=…, target=…,
flow_type=…'message')` по id, которые уже есть в схеме (перепись: названо 5/5,
применено 5/5, снято 5/5). Одна схема из пяти показывает и предел: дуга была
единственным продолжением шага внутри его пула, и после перевода в сообщение
`no_isolated` заряжает этот шаг. Это не ошибка совета, а обнажённый дугой тупик,
и подсказка теперь говорит это заранее (`_stranded_tail`): без предупреждения
оркестратор отклонил бы весь пакет через `_rules_regressed`, и дефект пережил бы
улучшение.

**Что показал живой ответ модели.** `warehouse_delivery.live.plan` — дословный
ответ GigaChat из прогона 21.09: 12 потоков, 5 из них ведёт из пула в пул, и
`kind="message"` не назван ни разу. До этого класса харнесс фиксировал на этой
фикстуре пять провалов и ни одним больше; теперь их шесть, и таблица
`EXPECTED_RAW_FAILS` дополнена с разбором. Это же объясняет, почему генерация в
replay не просела: `pass@1/flows_within_pool` и `pass@1/message_flow_ends` = 1.0
по итоговому XML (9 и 8 применимых схем) — нормализация на стадии записи XML
делает своё дело, и план-гейт теперь называет это знанием, а не удачей.

**Метрика, которая перестала быть честной без оговорки.** С ростом линейки
`improve/score_delta` упал с 0.5 до 0.0: знаменатель нормированного балла
вырос, и тот же пакет перестал двигать число. Контур не изменился — изменилась
мерка. Поэтому:

- `RunReport` теперь несёт `ruler` (правила с весами + список инвариантов),
  он же уезжает в baseline;
- при несовпадении отпечатков сверка пропускается с явной строкой
  «линейка изменилась … пересоберите baseline», и кода выхода у такой заметки
  нет — ровно как с несовпадением режимов;
- слепок **без** отпечатка (записан до этой меры) всё равно сверяется, но
  харнесс говорит об этом отдельно: «забыли ключ» не должно становиться способом
  выключить регрессии;
- baseline пересобран (`--write-baseline`) после того, как перепись корпуса и
  совпадение двух слоёв подтвердили: новые правила заряжают реальное, а не
  выдуманное.

### Очередь после десятого прохода

- `flow_ends_legal` оракула не имеет пары в скоринге: на корпусе класс пуст (0
  из 367), но аплайер режет такие дуги (шаг 1c), то есть модель их рисует.
  Решение отложено намеренно: правило, которое на реальных схемах не срабатывает
  ни разу, нельзя проверить переписью корпуса, и его слепоту тест не поймает.
- Операнд, которого нет: `pool_has_steps`, `sequence_flows`,
  `message_flow_ends`, `naming` и ещё восемь правил называют id, но просят
  значение, которое знает только автор. Доля «вне операций» в переписи —
  потолок этих правил, а не их дефект.
- `RECIPES_IN_MESSAGE = 3` остаётся единственным знаменателем «снято с одного
  пакета».
- Живой прогон (`--mode live`) здесь недоступен: нет `GIGACHAT_CREDENTIALS`.
  Все выводы этого прохода — про прочтение схемы, а не про ответы модели.

**Итог.** 1057 тестов. `eval.coverage`: нотационная таблица из 10 пар сходится
на 367 схемах целиком. `eval.advice --all-rules`: названо == применено, молчат
— [], сломано — 1 обнажённый тупик, названный в тексте подсказки. Прогон:
`pass@1/scenario` 0.545, балл 89.91, `scorer_oracle_agreement` 1.0, новые
инварианты 1.0.

## Ход одиннадцатого прохода: где искать классы, которых нет в корпусе

**Приём.** Рукописный датасет — источник ограничений: чего в нём нет, то
переписью `MUST_FIRE_ON_CORPUS` не находится никогда. Вторым источником данных
оказалась история самого контура: `eval/reports/*.json` хранят по каждому кейсу
вердикты оракула по итоговому XML (`checks_xml`), и их разбор по всем 136
отчётам (2080 проверок) даёт частоту дефектов, которые порождает именно модель,
а не человек.

**Что нашлось.** `flow_ends_legal` оракула падал 9 раз из 2080, а в скоринге
пары у него не было вовсе — то есть правило отсутствовало не потому, что класс
редкий, а потому, что сверка двух слоёв умеет находить расхождения только там,
где заведена пара. Три следствия, из-за которых молчание стоило дорого:
`_rules_regressed` пропускал пакет, нарисовавший дугу «из финиша в шаг»;
`_findings_block` не звал модель чинить то, что видит оракул; и загруженная
пользователем схема с такой дугой получала чистый балл.

**Правило.** `flow_ends_legal` (вес 8, 25-е по счёту, знаменатель 216): поток
не входит в `startEvent`/`boundaryEvent` и не выходит из `endEvent`. Теги
берутся из `SEQUENCE_FORBIDDEN_TARGETS`/`SEQUENCE_FORBIDDEN_SOURCES` аплайера —
линейка и гарант обязаны расходиться в прочтении схемы, а не в списке тегов.
Рецепта в подсказке нет намеренно: `disconnect` без замены создаёт тупик
(доказано переписью на `sequence_flows`), а чем замещать конец — называет автор
процесса; перепись из-за этого считает случай «операнд выбирает модель», а не
«молчит о причине».

**Честная оговорка.** На корпусе правило не срабатывает ни разу (и оракул
тоже), поэтому в `MUST_FIRE_ON_CORPUS` оно не занесено: тест требовал бы от
него находить то, чего в датасете нет. Его момент проверки — пары
`NOTATION_PAIRS` (11 пар, 0 расхождений на 367 схемах = правило не порождает
ложных нарушений) и unit-тесты на дугах, которых в корпусе нет.

**Регрессий не добавилось:** `pass@1/scenario` остался 0.545 (ни один
сгенерированный XML под новое правило не попал), `scorer_oracle_agreement` 1.0,
`structure_xml_agreement` 1.0. Отпечаток линейки сработал как задумано: прогон
сам объявил «линейка изменилась (24 правила, вес 208 → 25 правил, вес 216;
добавились: flow_ends_legal)» и отказался сверять балл, пока baseline не
пересобран после проверки на корпусе.

**Итог.** 1062 теста, 25 правил, 11 пар «скоринг ↔ оракул», две мерки
пополнения линейки: рукописный корпус и история прогонов контура.

### Побочная находка прохода: гигиена текста не доходила до документации

За проход трижды в markdown попала склейка кириллицы с латиницей
(`неMeasureются`, `молчитBoth`, ` artifact`) — ровно тот класс, от которого
тест `tests/test_source_hygiene.py` защищает `.py`, и ровно тот, который я
сам себе ловил в прошлых проходах. Причина: тест смотрел только
`app/`, `core/`, `eval/`, `tests/`, а `AGENTS.md` читается каждой следующей
сессией как инструкция, то есть мусор в нём живёт дольше, чем в коде.

Оба теста расширены на `*.md` в корне, `docs/**` и `.agents/**` (инлайн-код
вырезается, чтобы `flow_type='message'` не считался склейкой). Проверено на
месте: подсаженное `неMeasureлась` в план падает с указанием строки, после
отката — зелено. Долга в документации это не вскрыло (0 склеек в 16 файлах), а
первым же нарушением оказался цитатный абзац этого раздела: примеры склейки
обязаны стоять в инлайн-коде — там, где их не читает проверщик.

## Ход двенадцатого прохода: транспорт и текст правил перестали спорить с нотой

Проход не добавлял правил — он убирал то, что мешало уже найденным нарушениям
чиниться. Три дефекта одного семейства: **контур отказывал в правке, которая
нотой не запрещена**, и делал это так, что метрика считала её качеством модели.

### 1. Безымянный шлюз больше не считается ошибкой ответа

`naming` в линейке объявляет: имя обязательно шагу, а «шлюзу и событию имя в
BPMN не требуется — ветку подписывает условие». Тот же текст модель читает в
промпте улучшения — и всё равно получала `не задано имя элемента`: `_op_add_node`
требовал `name` у всех трёх видов узлов, `_op_add_boundary_event` — у события.

Найдено не догадкой, а записанным живым пакетом: в
`eval/fixtures/warehouse_delivery.live.improve.json` модель вернула
`add_gateway` с `name: ""`, и пропуск «шлюз без имени» стоит в провенансе
фикстуры как одна из четырёх отсеянных правок. Тип события скоринг читает из
дочернего `*EventDefinition` (`event_types`), а не из подписи, поэтому безымянный
шлюз или событие не добавляют схеме ни одного нарушения — отказывали законному.

Требование имени осталось только там, где его требует нота и линейка: у
активности (`add_task`). Узлу без имени атрибут `name` не пишется вовсе (пустая
строка в XML — мусор, который bpmn-js показывает как пустую подпись), а в отчёт
ходит пометка `имя не задано — шлюзу и событию в BPMN оно не требуется;
подписать узел можно операцией rename`. Пометка обязательна: тихая приемка
была бы той же тихой обрезкой, только наоборот.

**Что измерило харнесс.** `improve/op_acceptance` на живом пакете склада
0.6 → 0.667 (10 операций из 15 изменили схему вместо 9), средний по кейсам
0.8 → 0.833, `improve/score_delta` того же кейса +1 балл (88 → 89).
`defects_repaired` остался 0.0 — и это честное число: дефекты базовой схемы
(`event_types` на `A11`, два ожидания без срока) записанный пакет не убирает,
там модель предлагала `add_task` вместо таймера. Виновато содержание ответа, и
атрибуция это называет («дефект базовой схемы»), а не метрика.

### 2. Рецепт не имеет права печатать операнд перечислением

`event_types` советовал `add_event_definition(id='X', event_definition='message|error|signal')`.
Аплайер на такое значение отвечает `неизвестное определение события
'message|error|signal'`, то есть модель, скопировавшая подсказку буквально,
правку не приносила. Перепись `eval/advice.py` этого не видела: `_ALTERNATIVES`
перебирает варианты и засчитывает применимость по первому сработавшему, — и
рапортовал `назван 8/8 | применено 8/8` о тексте, неприменимом целиком.

Правило теперь называет **одно** значение (`_definition_choice`: самое частое из
допустимых этим событием — `message`, таймер с перечня снят по причине из
`_definition_options`), а остальные и оговорку выносит словами вне вызова
операции: «у этих узлов допустимы error, message, signal — определение выбирает
автор по смыслу шага, а таймер на самом ожидании снимает нарушение ценой
удалённого ожидания».

Измеритель исправлен вместе с текстом: у вердикта переписи завёлся отдельный
факт `verbatim` — принимается ли пакет, собранный из подсказки **без раскрытия
альтернатив**. Столбец `дословно применимо` печатается, когда он меньше
`назван`, а терпимость к перечислению больше не может прятаться: ratchet
`test_every_recipe_is_copy_pasteable_verbatim` требует равенства по всем
25 правилам на корпусе.

### 3. Id нового узла подбирается по схеме, а не печатается литералом

Второй круг починки читает подсказку со схемы, которую первый круг уже изменил.
Рецепт `approval_chain` звал `add_gateway(id='new_gate')`, а `wait_without_sla` —
`new_sla_timer_N` / `new_sla_fork_N`; как только такой id оказывался занят,
аплайер отвечал `id 'new_gate' уже занят` и нарушение не снималось ни одним
кругом. Перепись показывала это как `applied=True` первого круга с отказом
второго — то есть как качество модели, а не как текст правила.

Появился `_free_new_id`: берёт основу, пропускает занятые номера и даёт
свободный id, а счётчик живёт на один `evaluate`, чтобы несколько рецептов одного
прогона не выдали один и тот же узел. Префикс `new_` сохраняется — без него
аплайер отвечает «новый id без префикса new_».

**Эффект на реальных схемах (367 файлов, `python -m eval.advice --all-rules`):**

| правило | снято с одного пакета | снято за круги починки |
| --- | --- | --- |
| `approval_chain` | 6/8 (75%) → **8/8 (100%)** | 6 → **8** |
| `wait_without_sla` | 6/8 (75%) → 6/8 | 6 → **7 (88%)** |
| `event_types` | 6/8 → 6/8 (операнд назван верно) | 6 → 6 |

Терпимость переписи к перечислениям сохранена намеренно: она измеряет другой
вопрос — «был бы рецепт исполним, выбери автор правильно», — и отделена от
вопроса «скопировал бы модель его буквально».

### Проверки прохода

`python -m pytest tests/ -q` → 1068. `python -m eval.coverage` → 11 пар, 0
расхождений на 367 схемах. `python -m eval.run --mode replay
--fail-on-regression` → код 0, «Регрессий нет»: отпечаток линейки не менялся
(те же 25 правил, вес 216, тот же набор инвариантов), поэтому сверка была
 обязательна и засчитала рост `improve/op_acceptance` как улучшение, а не как
несопоставимость. Baseline пересобран после прогона, чтобы нижняя планка
0.833 сторожила и возврат транспортного отказа: верни кто-то требование имени
для шлюза — детектор это увидит.

### Остаток, который проход не закрыл

- `improve/defects_repaired = 0.0` на записанном живом пакете: чинит содержание
  ответа модели, а replay его не переигрывает. Нужен живой прогон
  (`GIGACHAT_CREDENTIALS` в этой среде нет).
- `event_types` и `wait_without_sla` снимаются не за один пакет на схемах, где
  нарушений больше `RECIPES_IN_MESSAGE = 3`: подсказка режется на три рецепта, и
  остаток доходит следующим кругом. Предел текста, а не ошибки — он и в
  докстринге переписи, и в колонке «снято за круги починки».
- `cross_pool_flow` на одной схеме корпуса после починки ломает `no_isolated` и
  `sequence_flows`: разорванная межпуловая дуга обнажает тупик, который схема
  держала в себе. Рецепт это называет (`_stranded_tail`), а не прячет.

## Ход тринадцатого прохода: у подсказки не бывает пустого операнда, а лимит замера перестал врать о рецепте

Линейка и оракул за этот проход не менялись (25 правил, вес 216, 11 пар) —
правилась **текстовая исполнительность** советов и честность одной колонки
переписи.

### 1. Пустой операнд в подсказке — это сломанный текст, а не «операнда нет»

Прямой обход корпуса (367 схем × все правила × `recommendations_by_rule`) на
паттерн `=''` и `''` дал два места, где правило печатало пустой id:

| правило | схем | что читала модель |
| --- | --- | --- |
| `pool_has_steps` | 36 | «шага нет, а вставлять его в дугу **””** нечего» |
| `sequence_flows` | 19 | «у дуги нет источника; дугу нужно приставить к шагу **””**» |

Второй случай был двойным: фраза всегда называла `sourceRef`, даже когда отсутствовал
как раз он — то есть указывала на несуществующий узел как на опору правки.

Что теперь: правило называет тот конец дуги, который на схеме **есть**
(`дугу нужно приставить к шагу 'X', а его сосед называется только по смыслу
процесса`), а если опоры нет — говорит это словами без кавычек («на схеме нет ни
одного конца этой дуги»). В `pool_has_steps` вместо `''→''` подставляется то, что
известно о пуле («в пуле есть старт 'S', но дуги старт→финиш нет» / «в пуле нет ни
старта, ни финиша»), и остаётся маркер `OTHER_LIMITS` — перепись по-прежнему
классифицирует такой случай как «операнд выбирает модель», а не как молчание.

Закреплено ratchet'ем по всему корпусу:
`tests/test_eval_coverage.py::TestAdviceTextHasNoEmptyOperands` — ни один текст
подсказки не должен содержать пустой операнд. Падает на 55 находках до правки,
зелён после.

### 2. «Снято за круги починки 0» перестало означать «рецепт не работает»

У переписи `eval/advice.py` кругов 5, а `RECIPES_IN_MESSAGE = 3`: на схеме с 45
безымянными событиями нарушение снимается не раньше 15-го круга. Прежняя колонка
показывала там 0 рядом с настоящим стопором совета, и эти два случая было не
отличить — то есть метрика приписывала тексту правила предел замера.

Добавлен отдельный факт `exhausted`: круги кончились, но каждый круг правка шла.
Печатается столбцом «не снялось за лимит кругов N» и пометкой в разборе схемы.
На корпусе сегодня: `event_types` — 2 из 8 (это и есть те самые 45-событийные
схемы), у остальных правил таких схем нет.

### Проверки прохода

1072 теста; `eval.coverage` — 11 пар, 0 расхождений; `eval.run --mode replay
--fail-on-regression` — код 0, «Регрессий нет» (числа харнесса не изменились:
`pass@1/scenario` 0.545, `improve/op_acceptance` 0.833, базовые дефекты живого
пакета по-прежнему `defects_repaired` 0.0 — проход касался текста советов, а не
гейта); `eval.advice --all-rules` — «дословно применимо == назван» по всем
правилам, молчаливых правил 0.

### 3. Совет не имеет права запрещать операцию и тут же её перечислять

Хвост «→ чинится: <операции>» дописывался к каждому упавшему правилу, даже
когда текст этого же правила говорил «правка не выражается операцией» /
«рецепт выражается только словами». Обход корпуса дал **312** таких текстов, из
них **295** — чистое противоречие (вызова операции в тексте нет, а список
операций прилеплен): `gateway_conditions` 154 схемы, `wait_without_sla` 60,
`start_event` и `end_event` по 37, `rework_loop` 24.

Модель читает хвост как разрешение выдумать операнд: единственный корректирующий
повтор уходит на правку, которую аплайер отвергнет, — то же семейство, что и
пустой операнд выше. Хвост теперь снимается, только если совет **отказался от
операции и не называет ни одного вызова**; в смешанном случае (одному нарушению
рецепт есть, другому нет — 17 текстов на корпусе) список операций остаётся,
потому что правка пакету доступна. Маркеры отказа вынесены в
`MANUAL_FIX_MARKERS`, детектор вызова — `_OP_CALL_RE` (та же форма, что у
`eval/advice.parse_recipes`, чтобы скоринг и измеритель видели исполнимость
одинаково).

Закреплено: корпусный `test_advice_that_denies_an_operation_does_not_list_one` и
перечисленные тесты `TestActionableRecommendations` (хвост ≤ 1 раз, всегда в
конце, отсутствие легально только при явно сказанном отказе).

### Что это изменило в числах

Числа харнесса не двинулись и не должны были: правлен текст советов, а не гейт
(`pass@1/scenario` 0.545, `improve/op_acceptance` 0.833, `defects_repaired` 0.0,
`business/*` без изменений). Двинулись числа переписей: пустых операндов 55 → 0,
противоречий «отказ + список операций» 295 → 0, лимит кругов переписи отделён от
нерабочего совета (`event_types`: 2 схемы «не снялось за лимит кругов»).

Разбор, найденный попутно: `end_event` на `Banking_example` применяет пакет за
кругом и остаётся `failed`, а на втором круге совет отказывается от операции —
потому что второй пул пуст (`pool_has_steps`). Это не дыра рецепта, а порядок
правил: сначала шаг, потом его финиш. Так и сказано в тексте подсказки.

## Ход четырнадцатого прохода: класс «узел есть, а его содержимое пустое»

### 1. Таймер без хронометража: 110 схем из 367 не видели ни один слой

Правило поиска осталось то же, что в тринадцатом проходе, но повёрнутое: не
«какой класс узлов не назван», а «где ищущий читает наличие узла, а нота требует
его содержимое». Так нашёл `timerEventDefinition` без `timeDate` /
`timeDuration` / `timeCycle`.

Корпус (367 рукописных схемы, Signavio-экспорт):

```
timer_without_schedule ругает 110 схем | названо элементов 1399 |
  нарушители: intermediateCatchEvent×718, startEvent×680, boundaryEvent×1
```

Свидетели проверены глазами в файлах, а не по регэкспу: `Exercise_3_*` —
`<startEvent name="Reminder activated">` с определением таймера и пустым
набором хронометража; `ex6_*` — `name="Await Turn"` там же.

Почему раньше молчали оба слоя, а не один:

- `event_types` требует дочерний `*EventDefinition` — он на месте, нарушения
  этого правила нет;
- `wait_without_sla` требует таймер **или** параллельную ветку «срок вышел» —
  таймер формально есть, и оркестратор улучшения считал ожидание защищённым;
- по BPMN событие с `timerEventDefinition` без одного из трёх элементов не
  заведётся никогда: исполнитель не знает, когда сработать. То есть ветка
  «срок вышел» не наступает, и `waits_have_sla` обещала ровно то, чего не
  меряла: не наличие кружка, а наступаемость срока.

Закрыто четырьмя слоями сразу, чтобы класс нельзя было «снять» правкой одного:

- **линейка** (`core/bpmn_scoring.py`): правило `timer_without_schedule`, вес 8 —
  26-е по счёту, вес линейки 216 → 224. Рецепт называет единственный легальный
  операнд: `add_event_definition(id='X', event_definition='timer',
  duration='PT15M')`, и тут же говорит, что `PT15M` — значение по умолчанию, а
  срок по SLA процесса называет автор.
- **аплайер** (`core/bpmn_edits.py`): экспорт `timer_schedule(definition)`
  (прочитать хронометраж из определения, `""` если пусто) и `_op_add_event_definition`,
  который умеет **заполнить** инертный таймер. Отказывается от двух удобно
  выглядящих поступков: перезаписать уже заданный хронометраж и сменить тип
  определения — и то, и другое было бы молчаливой подменой события в пакете.
- **независимый оракул** (`eval/invariants.py`): инвариант `timer_schedule`
  (12-й в `NOTATION_INVARIANTS`), `_Node.schedule`. Независимое прочтение, тот же
  смысл, другие исходники.
- **покрытие** (`eval/coverage.py`): пара `timer_without_schedule ↔
  timer_schedule` и правило внесено в `MUST_FIRE_ON_CORPUS` — «ни разу не
  сработало на реальных схемах» больше нельзя объяснить тем, что класса просто
  нет в данных: 110 схем на корпус против 0 расхождений.

### 2. Ошибка оракула, найденная на собственном фикстуре

Первая версия `_graph_from_xml` читала `timeDate|timeDuration|timeCycle` прямым
ребром события. В BPMN они вложены **внутрь** `*EventDefinition`, поэтому
`schedule` оказывался `""` всегда: оракул ругал легальные схемы, и фикстура
`FLOW_ENDS_XML` (граничное событие с `PT1H`) не проходила, даже когда стала
корректной. Починено чтение; урок записан в тесты: инвариант проверяется и
фикстурой, где нарушение снято законной правкой, иначе «оракул строгий» неотличим
от «оракул читает не там».

### 3. Стадию плана нельзя наказывать за срок, который поставляет транспорт

Генератор подставляет `DEFAULT_TIMER_DURATION` сам, поэтому план-гейт не может
«забыть» хронометраж. В `_graph_from_structure` таймер без срока в плане помечается
сентинелом `"plan-default"`, и `timer_schedule` его не считает: иначе
`pass@1/timer_schedule` мерил бы поведение транспорта, а не ответ модели. По XML
сентинел не действует — там видно, что реально в файле.

### Проверки прохода

- `python -m pytest tests/ -q` → **1095 passed** (1073 → 1095: 8 тестов
  `TestTimerWithoutSchedule` + исполнимость рецепта в `TestActionableRecommendations`,
  5 тестов заполнения инертного таймера в `TestEventDefinitionOnExistingEvent`,
  8 тестов `TestTimerSchedule` у оракула).
- `python -m eval.coverage` → 12 пар, `timer_without_schedule сошлись 367 |
  разошлись 0`; `MUST_FIRE_ON_CORPUS` ни одним правилом не нарушен.
- `python -m eval.run --mode replay` → новый гейт `pass@1/timer_schedule = 1.0`
  (применимо 5 кейсов), `pass@1/scenario` 0.545 — без изменений: ни один упавший
  кейс не падал из-за инертного таймера, и в этом смысл проверки, что гейт не
  ослаблен, а расширен.
- `python -m eval.advice --all-rules` → `timer_without_schedule`: названо 8/8,
  применено 8/8, дословно 8/8, снято одним пакетом 8/8 (100%), регрессий 0.
  Лучший ряд переписи: операнд у правила один, и он читается из схемы.
- Отпечаток линейки (`ruler_fingerprint`) корректно сообщил, что baseline
  несопоставим (25 правил, вес 216 → 26 правил, вес 224; добавились
  `timer_without_schedule`, инварианты: `timer_schedule`), сравнение пропущено;
  baseline пересобран, `--fail-on-regression` против него чист.

### Остаток, который проход не закрыл

- `cross_pool_flow`: на `schufa_*` правка снимает нарушение и ломает
  `no_isolated` + `sequence_flows` (единственная строка переписи с регрессией).
  Причина не в рецепте, а в том, что перенос межпуловой дуги лишает узел
  единственного входа; подсказка не требует второй правки. Отложено.
- `event_types`: 2 схемы «упоролся в лимит кругов переписи» (5) — рецепт рабочий,
  но на схеме с десятками событий его не хватает кругов; это потолок замера, а не
  совета, и он подписан отдельной колонкой.
- `boundary_events`, `flow_ends_legal`, `role_pools` по-прежнему не срабатывают
  на корпусе — проверено, что это отсутствие класса в данных, а не мёртвый код
  (`MUST_FIRE_ON_CORPUS` их не требует, тесты оракула и аплайера на них есть).

## Ход пятнадцатого прохода: правка не имеет права сама становиться дефектом ноты

### 1. Развилка не бывает концом потока-сообщения

Нашлось не корпусом, а собственной подсказкой. Перепись `eval/advice.py`
показывала по `cross_pool_flow` строку «снято с одного пакета 5 (100%)», и лишь
в колонке «сломано другое правило» — одну схему. Разбор пакета
`schufa_-_english_*` дал:

```
applied=4 reverted=''  снято=['cross_pool_flow']  новые=['no_isolated', 'sequence_flows']
connect(source='sid-BE7F2E5B…', …, flow_type='message')   # источник — exclusiveGateway
```

Два факта, и оба про сам совет:

- конец обмена — развилка. Замер по корпусу: из **625** `messageFlow` в 367
  рукописных схемах ни один не подходит концом к шлюзу (концы: task×609,
  intermediateCatchEvent×213, startEvent×160, intermediateThrowEvent×87,
  endEvent×29, sendTask×10, manualTask×1). По BPMN у обмена конец — участник или
  узел, но не шлюз.
- перевод дуги в сообщение обнажал приёмник: у `Perform Level 2 Scoring` не
  оставалось входа последовательности, и `no_isolated` с `sequence_flows`
  заряжали следом (предупреждение `_stranded_tail` в тексте был, но совет это не
  останавливало).

Класс закрыт в трёх слоях, чтобы совет не мог его породить:

- **линейка**: `message_flow_ends` теперь заряжает «конец — развилка» (вес
  прежний, 8: это та же граница, а не новое правило); `cross_pool_flow` больше не
  печатает `connect(source='<шлюз>', …)` — `_message_leg` берёт отправителем
  единственный шаг, ведущий в развилку (приёмником — единственный выходящий), а
  если таких шагов не один, рецепт ограничивается `disconnect` и говорит, что
  операнд называет автор процесса.
- **аплайер**: `_op_connect` с `flow_type=message` отвергает шлюз на конце с
  причиной и подсказкой про законную форму. Это не обрезка валидного ответа:
  такой обмен невалиден.
- **онтрольный оракул**: `message_flow_ends` заряжает тот же класс
  независимым прочтением; пара сверяется на корпусе (367/0).

Закрепляющие проверки в `tests/test_eval_coverage.py`: ни один обмен корпуса не окончен
шлюзом (правило не ругает рукописные схемы) и ни один разобранный рецепт
`cross_pool_flow` не ставит шлюз концом обмена.

### 2. Эталон прятал узкое место за нотационной ошибкой

Строже оракул — и `test_good_plans_satisfy_all_declared_invariants` поймал
собственный эталон: в `product_return.good.plan` заключение экспертизы стояло
`M3: C1 → G3`, то есть ответ внешнего центра приходил прямо в развилку
«Экспертиза подтвердила брак?». Это не случайность фикстуры, а способ, которым
она уклонялась от `pools_not_pingpong`: если вернуть ответ тому же шагу `A5`,
который его запросил, получается тот самый перекид, уже задокументированный для
`loan_application`.

Измерены три варианта правки (оракул, оба слоя):

| вариант | нотация | бизнес-узкие места |
|---|---|---|
| `M3 → A5` | чисто | `pools_not_pingpong` |
| `M3 → W1` (событие ожидания) | чисто | `waits_have_sla` |
| `A5 → A9 «Принять заключение» → G3`, `M3 → A9` | чисто | нет |

Выбран третий: он чинит маршрут, а не прячет дефект, и не требует ни таблиц
долга, ни смягчения гейта. Таблицы `EXPECTED_ETALON_BUSINESS_DEBT` и
`EXPECTED_NEW_FAILURES` остались в прежнем составе — правка эталона не вывела
из них ни одного имени и не добавила в них ни одного.

### Проверки прохода

- `python -m pytest tests/ -q` → **1109 passed** (было 1095; +14: 5 тестов
  линейки, 4 аплайера, 3 оракула, 2 корпусных регнетча).
- `python -m eval.run --mode replay --fail-on-regression` → **код 0, «Регрессий
  относительно baseline нет»**. Оракул стал строже, а baseline остался
  сопоставим (набор правил и инвариантов не менялся): `pass@1/scenario` 0.545,
  `pass@1/message_flow_ends` 1.0 (8 применимых), `business/smells` 0.455,
  `pools_not_pingpong` 0.875, `improve/op_acceptance` 0.833.
- `python -m eval.coverage` → 12 пар, 0 расхождений, «кроме объявленных в
  замысле: пусто».
- `python -m eval.advice --all-rules` → `cross_pool_flow`: названо 5/5,
  применено 5/5, дословно 5, снято 5 (100%); на `schufa_*` пакет теперь `ops=2`
  (два `disconnect`) без попытки посадить обмен на шлюз. Остаток честно виден:
  `регрессии=['no_isolated', 'sequence_flows']` — продолжение внутри пула
  получателя знает только автор процесса.

### Остаток, который проход не закрыл

- `cross_pool_flow` по-прежнему оставляет приёмника без входа там, где у
  пула-получателя нет готового маршрута до него: правка на один пакет не
  закрывается, и подсказка не вправе выдумывать продолжение.
- Поток-сообщение, оканчивающийся **конечным событием** другого пула (`schufa_*`:
  `G1 → endEvent`), после разъединения остаётся «холодным» финишем. Нота его
  разрешает, линейка не заряжает, но как сигнал узкого места он читается хуже,
  чем как дефект.
- `message_flow_ends`: операнда по-прежнему нет (6/6 «операнд выбирает модель») —
  конец обмена без описания процесса не назовёшь.

## Ход шестнадцатого прохода: отказ совета обязан совпадать с тем, что правда отвергает аплайер

Подозрение пришло из переписи: `start_event` показывал «вне операций 8» — то
есть правило восемь раз отказывалось называть правку. Проверка утверждения
«`add_event` тут неприменим» дала три факта.

1. Из 49 пулов корпуса без стартового события **37 не имеют ни одного шага**, и
   на них аплайер действительно отвечает `пул не определён` — отказ честный.
2. **12 пулов со шагами** совет и раньше оснащал рецептом `add_event(...) +
   connect(...)` — то есть недооценки инструмента не было.
3. Голый `add_event(participant='X')` в пул, где шаги есть, применяется и снимает
   `start_event`, но тут же заряжает `no_isolated`: стартов без ноги — это
   украшение схемы. Значит, рецепт обязан требовать дугу, а не только кружок.

Первая формулировка нового текста напечатала `connect(source='new_start_1',
target='<шаг пула>')` с плейсхолдером. Это тот же грех, что и пустой операнд
прохода тринадцатого: модель копирует текст дословно, аплайер отвечает «источник
или цель не найдены». Оставлен вызов, который применим целиком
(`add_event(id='new_start_N', event_type='start', participant='X')`), а
обязательная второй правкой дуга описана словами с указанием, что без неё старт
изолирован.

Отдельная находка, ради которой всё и измерялось: **11 пулов со шагами, у которых
нет ни одного свободных входа** (маршрут замкнут или кормится сообщением).
`_route_entry` возвращал для них `''`, и совет печатал «в пуле нет ни одного
шага» — причину, которой не существует. Теперь ветка различает два случая, и это
заперто корпусным ratchet: `TestDenialMatchesTheApplier` обходит все 367 схем и
требует, чтобы отказ звучал ровно там, где в пуле нет активностей, и только там.

Приём прохода (повторяемый): подозрение на «совет недооценивает инструмент»
проверяется не чтением текста, а вызовом аплайера на реальных схемах — применимо /
отказано / какие правила зарядилось следом. Так же проверяется и обратное: если
измерение подтверждает отказ, находка всё равно закрепляется тестом, чтобы
отказ не протух в ложь при следующем изменении линейки.

### Проверки прохода

- `python -m pytest tests/ -q` → **1110 passed** (+1: корпусный
  `test_start_event_denies_only_pools_without_steps`).
- `python -m eval.run --mode replay --fail-on-regression` → **код 0, регрессий
  нет**: правлен текст подсказки, гейт не тронут.
- `python -m eval.coverage` → 12 пар, 0 расхождений.
- `python -m eval.advice --all-rules` → строка `start_event` не двинулась
  («вне операций 8»): все восемь отобранных нарушений — пустые пулы из 37, где
  отказ правда честный.

## Ход семнадцатого прохода: самый массовый класс корпуса стал починимым

Пункт очереди «холодный финиш» проверен и **снят**: конечных событий без единого
входящего потока в корпусе 3936 в 80 схемах, и это демо элементов нотации и
пустые шаблоны упражнений (файлы `compensate*`, `Exercise_*`, `ex6_*`), где
несоединённые кружки — замысел, а не брак. Правило, которое ругает 80 рукописных
схем, добавляло бы ложные нарушения, а не эффективность.

Взято другое: `task_types` — самый массовый класс линейки (200 схем из 367,
1807 родовых `task`), и он был **неисправим в принципе**: у аплайера не было
операции, меняющей тип существующего узла. Модель, правильно ответившая
«это serviceTask», получала только `delete` + `add_task` — с новым id, потеряй
дорожки и потерянной ссылкой диаграммы, а `_rules_regressed` заряжал оборванный
маршрут. Операционный словарь промпта (`_operations_block()` рендерит `OP_SPEC`
дословно) про такую правку просто не знал.

Сделано:

- `core/bpmn_edits.py`: `TASK_TYPE_TAGS` (восемь родов задачи; `subProcess` и
  `callActivity` исключены — это другой элемент с содержимым, а не переименованный
  шаг) и `_op_set_task_type`: меняет тег, сохраняя id, имя, дорожку и обе дуги;
  короткая форма (`service`) равна полной (`serviceTask`), потому что отказ по
  форме имени — обрезка валидного ответа транспортом; повтор того же типа —
  noop-строка отчёта; шлюз и событие получают отказ с причиной «не шаг».
- `OP_SPEC` + `_HANDLERS`: 18 операций → 19, словарь модели и аплайер снова
  совпадают.
- `core/bpmn_scoring.py`: `_task_type_evidence` читает тип из структуры
  коллаборации — у шага с потоком сообщения наружу это `sendTask`, у шага,
  которому отвечают, `receiveTask`. Совет называет `set_task_type(id=…,
  task_type=…)` только там, где операнд выводится из дуг; остальным говорит, что
  тип называет автор, и **не печатает вызов с выдуманным значением** (урок
  плейсхолдера из прохода 16).

### Что это изменило в числах

Перепись `eval/advice.py --all-rules`, строка `task_types`: было «рецепт назван
0/8 | применено 0/8 | операнд выбирает модель 8» → стало **назван 5/8, применен
5/8, дословно применим 5, снято с одного пакета 5 (62%), сломано другое
правило 0**, и честно 3/8 «операнд выбирает модель». Гейт не шевельнулся:
`python -m eval.run --mode replay --fail-on-regression` → **код 0, регрессий
нет** (операция добавлена в инструмент, а не в мерилку), покрытие — 200 схем по-
прежнему ругаются, расхождений скоринг↔оракул ноль. Тестов **1119**.

### Очередь после прохода

- 62% снятия по `task_types` — потолок структуры: там, где типа-подсказки в дугах
  нет, правку делает только ответ модели по описанию процесса.
- `sendTask`/`receiveTask` теперь читаются из коллаборации; `manualTask` и
  `businessRuleTask` структурных признаков не имеют и остаются на авторе.
- Живой прогон (`defects_repaired`, `op_acceptance` на настоящем пакете) по-прежнему
  недоступен без `GIGACHAT_CREDENTIALS`.

## Ход восемнадцатого прохода: аудит утечки перестал полагаться на имя файла

Требование «метрики не считаются на тестовых данных» держалось проверкой двух
вещей: фикстура не совпадает с few-shot образцом промпта, и в блок практик не
пришёл эталон **того же процесса**. Второе узнавалось только по имени файла
корпуса (`names_the_scenario`) — а корпус рукописный и именован как попало, так
что тот же процесс под другим заголовком (например `Warenversand_*` вместо
`warehouse_delivery`) прошёл бы аудит незамеченным. Это ровно та дыра, из-за
которой `improve/*` может мерить воспроизведение подобранного ответа.

Сделано в `eval/provenance.py`:

- `etalon_step_names` снимает с эталонного ответа названия шагов, у которых два
  и более значимых слова (односложная метка есть в каждом втором процессе);
- `scenario_findings(..., etalon=…)` сравнивает блок практик с этим списком по
  **целым названиям** (`mentions`), а не по доле совпавших слов, и находит
  утечку, когда блок воспроизводит половину маршрута (`ETALON_NAME_LEAK = 0.5`,
  `ETALON_MIN_NAMES = 4`, минимум два имени);
- `audit()` собирает эталоны по приоритету качества (`good` → `real` → `bad`),
  потому что первая редакция брала только «good» и ослепала на двух сценариях
  набора, где good-плана нет;
- отчёт несёт `etalon_scenarios` и `scenarios`, и `format_findings` печатает
  полноту даже при нуле находок: «находок нет» от аудита, видевшего половину
  набора, и от аудита по всему набору — разные утверждения.

Замер: **8 из 8** сценариев участвуют в содержательной сверке, находок — 0, то
есть набор кейсов чист и по именам, и по содержанию. Метрики харнесса не
двинулись: правлен аудиторный слой, а не оракул (`pass@1/scenario` 0.545,
`business/smells` 0.455, `--fail-on-regression` → код 0).

Тесты: три unit-проверки (половина эталона в блоке → находка; чужой процесс с
той же доменной лексикой → чисто; короткий эталон → не участвует) и одна на
полноту охвата (`bad`-план тоже даёт эталонные имена). Тестов **1123**.

### Очередь после прохода

- Содержательная сверка смотрит на названия шагов; процесс, переименованный
  до последнего слова, но сохранённый структурой (те же id и тот же граф),
  останется невидимым. Следующий шаг — сверка графа маршрута, а не имён.
- Блок практик в провенансе приближается `_format_practices` без генерации
  XML: это то же приближение, что и в модуле, а не живой промпт.

## Ход девятнадцатого прохода: утечка мерится по содержимому подборки, а не по тому, что влезло в промпт

Прошлая содержательная сверка (`ход 18`) сравнивала эталон кейса с **блоком
промпта**. Это была неверная поверхность: `_format_practices` кладёт в блок только
фразы практик и имя файла, а шаги подобранной схемы живут в поле хита
`element_names` — то есть дубликат процесса под чужим заголовком остался бы
невидим, потому что в блок его имена просто не попадают.

Перемерено по факту, а не по предположению: снят реальный состав хита
(`name`, `domain`, `complexity`, `practices`, `xml_features`, `element_names`,
`similarity`, `missing_practices`), после чего

- `rag_surfaces` несёт `element_names` и `xml_features` каждого хита;
- `scenario_findings` сравнивает с эталоном кейса **целые названия шагов
  подобранной схемы** (`mentions`, доля ≥ `ETALON_NAME_LEAK`, минимум два
  совпадения) и заводит находку «подобранный эталон несёт шаги процесса кейса»;
- `overlap_margin` + поле `closest` в отчёте: «находок нет» печатается вместе с
  расстоянием ближайшего подбора до порога. Без этого числа ноль совпадений
  неотличим от мёртвого детектора (тот же ноль даёт пустое `element_names`), и
  тест `test_rag_surfaces_actually_carry_element_names` держит доставку данных
  отдельно от порога.

Замер на реальном наборе: 8 из 8 сценариев под сверкой, находок 0, **ближайший
подбор — 0% совпадений шагов** при пороге 50%. То есть набор кейсов чист и по
именам файлов, и по содержимому подборки, и это видно как число, а не как молчание.

Тестов **1127** (+4: срабатывание по `element_names`, чистый случай чужого
процесса, доставка данных детектору, расстояние в отчёте). Гейт `--fail-on-regression`
→ код 0: правлен аудиторный слой, оракул и фикстуры не тронуты.

### Очередь после прохода

- Дистанция 0% означает, что порог сегодня не нагружен: проверка — страховка на
  случай, когда в корпус попадёт процесс той же формы с теми же шагами. Чтобы
  страховка не протухла, её стоит нагружать примером в тестах (сделано), а не
  ждать живой утечки.
- Следующий уровень той же задачи: сравнение **графа маршрута** (мультимножество
  дуг `(kind(source), kind(target))` + счётчики родов узлов) — переименованный до
  последнего слова процесс сегодня прошёл бы и мимо имён шагов.

## Ход двадцатого прохода: форма маршрута как вторая, независимая от имён опора аудита

Сверка по `element_names` (ход 19) ловит дубликат, пока в нём сохранены названия
шагов. Процессы переименовывают целиком — тогда в подборке остаётся та же схема
с другими словами, и ноль совпадений по именам означает не чистоту, а слепоту.
Добавлено сравнение **формы маршрута**: мультимножество дуг
`(род источника, род цели)` с счётчиками повторных ног, без имён и id.

Пороги и их обоснование: `MIN_ROUTE_EDGES = 8` (шесть дуг совпадают у половины
упражнений корпуса, и это форма учебников, а не утечка) и
`ROUTE_SIMILARITY = 0.8` по жаккарду мультимножества. Фикстура для сравнения
берётся та, у которой маршрут богаче, — иначе нулевое расстояние означало бы
«сравнивать нечего».

Калибровка сделана до того, как нулям поверили:

- самоподобие плана = 1.0;
- клон с переименованными id, узлами и подписями (та же форма) = 1.0;
- 48 из 48 имён хитов переводятся в файл корпуса, то есть данные у сверки есть;
- реальные расстояния набора: 0.000 у семи сценариев, 0.027 у
  `loan_application` при пороге 0.80 — находок нет, и это число видно в отчёте
  («форма маршрута — 3% дуг (порог 80%, кейс loan_application)»), а не молчание.

Стоимость: полный аудит 1.1 с (48 разборов XML), что по-прежнему годится для CI.

Тестов **1132** (+5: клон survives, малый маршрут не участвует, самоподобие,
находка на уровне `audit`, разрешение имён хитов). Гейт `--fail-on-regression`
→ код 0: оракул, фикстуры и метрики харнесса не тронуты, добавлен только слой
доказательства чистоты набора.

### Очередь после прохода

- Форма маршрута не видит переставленные дорожки и добавленные шаги-обёртки
  (симуляция «тот же процесс, но с подпроцессом»): следующий уровень — сравнение
  с нормализацией подпроцессов, а не сырых тегов.
- `route_margins` считается по верхней границе similarity, но не хранит, с каким
  именно файлом корпуса это сравнение произошло: при находке пригодится имя.
- Остальное без изменений: `task_types` 62%, орфанный приёмник в
  `cross_pool_flow`, `RECIPES_IN_MESSAGE = 3`, live-прогон без ключей.

## Ход двадцать первого прохода: расстояние сверки обзаводится адресом, а два пункта очереди закрыты измерением

Пункт «сохранить имя схемы корпуса» выполнен: `route_margin` возвращает пару
`(доля, имя)`, аудит кладёт имя в `route_files` по каждому сценарию, в
`closest_route.file` и в `value` находки (`x.good.plan ← clone.bpmn`), а строка
отчёта печатает адрес: «форма маршрута — 3% дуг (порог 80%, кейс
loan_application, ближайшая схема корпуса —
`3_56a29c753e8648b7aa9c4715e6471363.bpmn`)». Имя — ровно то, что поисковик
принёс в промпт, а не первое из подборки: оно записывается только вместе с
максимумом, поэтому адрес без числа не появляется (тест
`test_route_margin_without_a_route_names_nothing`).

Два пункта очереди закрыты не правкой, а измерением — и это результат, а не
отложенное решение:

- **нормализация подпроцессов не нужна**: снимок формы строится обходом всего
  дерева (`root.iter()`), то есть узлы и дуги внутри `subProcess` уже считаются;
  в корпусе 367 файлов нет ни одной дуги внутри подпроцесса (замер по
  `subProcess` → 0 файлов, 0 дуг), так что сравнивать «тот же процесс с
  обёрткой» пока не на чем. Проверка была бы мертва на входе.
- **шаг без исполнителя** как кандидат в инварианты не проходит ту же планку, что
  и молчаливые правила: 0 шагов без пула и без дорожки в корпусе (1840 шагов) и 0
  в 11 планах eval-набора. Добавлять правило, которое не звенит ни на каких
  данных, — тот же дефект, который `MUST_FIRE_ON_CORPUS` ловит у скоринга.

Зато замер нашёл настоящий пробел: предупреждение об обнажённом приёмнике в
`cross_pool_flow` (`_stranded_tail`) считалось симметричным и было, но под
тестом висела только половина — про отправителя. Добавлен
`test_advice_warns_when_the_cut_strands_the_receiver`: шаг, у которого дуга из
чужого пула была единственным входом, назван в рецепте, а отправитель при этом
не попадает в предупреждение зря.

Тестов **1134** (+2: адрес находки и приёмник). Гейт
`python -m eval.run --mode replay --fail-on-regression` → код 0, регрессий нет;
оракул, фикстуры и пороги не тронуты.

### Очередь после прохода

- Бизнес-слой оракула: кандидаты «шаг без исполнителя» и «нормализация
  подпроцессов» отсеяны измерением (см. выше), зато замер нашёл живой и
  неизмеряемый класс. Из пяти бизнес-правил линейки (`approval_chain`,
  `handoff_pingpong`, `lane_overload`, `rework_loop`, `wait_without_sla`) четыре
  отражены в `BUSINESS_INVARIANTS`, а `approval_chain` — нет. При этом правило
  не молчит: по корпусу 37 файлов его нарушают, 137 выполняют, 193 — не применимо
  (`details`/`details_meta` у `BPMNScorer.evaluate`). Значит `business/smells` и
  `improve/business_repaired_share` слепы к тому классу узких мест, который
  линейка находит чаще остальных: улучшение по согласованию в метрику не
  превращается.
- Следующий проход: завести пятым бизнес-инвариант оракула по тому же свойству,
  независимой реализацией (оракул не имеет права переиспользовать код линейки), и
  прогнать его по эталонам — если эталонный план нарушает согласование, дебит
  ложится в `EXPECTED_ETALON_BUSINESS_DEBT`, а не под ковёр. Перед правкой
  сверить, что `business/smells` после изменения знаменателя не читается
  регрессией: значение метрики изменится из-за ужесточения измерения, и это нужно
  задокументировать в baseline, а не «починить» ослаблением порога.
- `task_types` 62% — потолок по operand'у: тип называет модель, структура его не
  доказывает; дальше только за пределы детерминированного контура.
- `RECIPES_IN_MESSAGE = 3` — колонка `exhausted` считает хвост списка, но сами
  рецепты четвёртого и дальше участника в промпт не попадают.
- Live-прогон (`improve/defects_repaired`, настоящий `op_acceptance`) невозможен
  без `GIGACHAT_CREDENTIALS`.

## Ход двадцать второго прохода: у бизнес-слоя оракула появился пятый инвариант

Пункт очереди из двадцать первого прохода закрыт правкой, а не измерением.
Сверка множеств показала, что из пяти бизнес-правил линейки четыре отражены в
`BUSINESS_INVARIANTS`, а `approval_chain` — нет. Перепись по корпусу подтвердила,
что правило живое: 37 схем из 367 нарушают, 137 выполняют, 193 не применимо.
Значит `business/smells`, `business/not_worse_than_etalon` и
`improve/business_repaired_share` не видели этого класса узких мест: улучшение
цепочки согласований не превращалось ни в какое число.

Решение отменено осознанно. В `tests/test_eval_invariants.py` годами стояла
запрещающая строка «`approval_chain` остаётся без независимой проверки: четыре
согласующих — много, и из семантики BPMN это не выводится». Аргумент был
настоящий, но он уже не применяется к этому слою: `waits_have_sla` и
`no_overloaded_lane` держат бизнес-пороги (срок в модели, 75% работ на дорожке),
которые из ноты не следуют, и независимость там понимается как **другой способ
прочитать схему**, а не как другой признак. Поэтому оракул идёт по `seq_out`
своего `_Graph` (`_signoff_chain` — поиск линии длиной ≥4), а не по
`_longest_user_task_line` над `_Schema`, и именно это сверяет перепись. Порог
(`SIGNOFF_CHAIN_MIN = 4`) и набор ручных типов (`HUMAN_SIGNOFF_KINDS`, включая
безымянный по типу `task` — 212 нарушений из 213 на корпусе) совпадают со
скорингом намеренно: расхождение по одному бизнес-вопросу означало бы, что
продукт меряет себя сам.

Что замерено после правки:

- перепись `python -m eval.coverage`: пара `approval_chain ↔ signoffs_need_a_gate`
  сходится на **367 из 367** схем, расхождений 0; из них 37 с находкой с обеих
  сторон, то есть согласие не пустое (слепое зеркало дало бы `scorer_stricter`).
- тесты оракула на новом инварианте (7): четыре подписи в дорожке → заряжены id
  `A1…A4`; развилка после третьей → снято; `serviceTask` в середине → ручных
  меньше четырёх, свойство неприменимо; три подписи → неприменимо; безымянный
  `task` → считается; процесс без `laneSet` → не прощается, а заряжается;
  последняя пара держит тот же договор против скоринга на одной схеме.
- фикстуры: ни один эталон и ни один план набора новый инвариант не нарушает,
  поэтому `EXPECTED_ETALON_BUSINESS_DEBT` и `EXPECTED_NEW_FAILURES` не
  пополнялись; в `not_applicable` синтетического однопулового плана добавилось
  пятое имя (три ручных шага).
- числа харнесса: `pass@1/signoffs_need_a_gate` = 1.0 при 5 применимых случаях,
  `business/smells` 0.455, `business/scorer_oracle_agreement` 1.0 — то есть
  измерение стало строже, а contour не изменился. Это ожидаемый исход прохода
  точности: в replay-наборе цепочки согласований нет.
- отпечаток линейки честно объявил baseline несопоставимым («инварианты:
  signoffs_need_a_gate»); после `--write-baseline` гейт даёт код 0.

Тестов **1141** (+7 на новый инвариант, +2 в двадцать первом). Проверки:
`python -m pytest tests/ -q` → 1141 passed;
`python -m eval.run --mode replay --fail-on-regression` → код 0;
`tests/test_source_hygiene.py` → 3 passed.

### Очередь после прохода

- Новый бизнес-инвариант не имеет ни одного нарушения в eval-наборе: чтобы
  `improve/business_repaired_share` по согласованию стал измеряемым, нужен кейс,
  где базовая схема держит четыре подписи подряд (данные, а не порог).
- Остальное без изменений: `task_types` 62%, `RECIPES_IN_MESSAGE = 3`,
  live-прогон без `GIGACHAT_CREDENTIALS`.

## Ход двадцать третьего прохода: систематическая перепись правил без независимой проверки

Вместо поиска очередного класса «на глаз» сравнены множества: 26 правил линейки
против 12 пар `NOTATION_PAIRS` и 5 пар `SCORING_TO_ORACLE`. Без зеркала в оракуле
оставались девять: `documentation`, `element_count`, `end_event`,
`guarded_cycles`, `naming`, `pool_lanes`, `sequence_flows`, `start_event`,
`task_types`.

Дальше вопрос не «сколько их», а **проходит ли гейт схему с таким дефектом**.
Замер по всем планам набора (тот же путь, что у прогона: `repair_structure` →
`generate_xml` → `check_xml`): `start_event`, `end_event`, `guarded_cycles`,
`naming`, `pool_lanes`, `sequence_flows`, `element_count` не нарушает ни один план
набора. `documentation` нарушают все шесть эталонов, `task_types` — два
(`product_return.good.plan`, `purchase_approval.good.plan`). То есть дыра в
приёмке на этих данных только одна, и зеркалить мягкие правила (подпись, тип
шага, количество элементов) означало бы требовать от модели то, чего не держит
сам эталон, — тот же класс ошибки, что ослаблять оракул.

Закрыто то, что действительно пропускалось: **безусловный цикл**. Правило
`guarded_cycles` ругает 28 схем корпуса из 367 (нарушители:
`intermediateCatchEvent`×28, `eventBasedGateway`×27, `task`×25), а оракул про
механизм выхода из цикла не спрашивал вообще. Добавлен
`loops_have_a_guard`: цикл ищется обходом `seq_out` с путём-стеком по `_Graph`
(обратная нога замыкает участок пути — это и есть цикл), защищён он, если внутри
лежит `exclusiveGateway` с двумя и более ногами. Подпись дуги критерием здесь
сознательно не считается: её наличие спрашивает `no_blind_rework`, тут
спрашивается механизм ветвления.

Что измерено:

- перепись пары `guarded_cycles ↔ loops_have_a_guard`: 39 схем, где свойство
  существует, — **согласие**; 28 из них с находкой с обеих сторон; 328 —
  `not_comparable` (ациклические схемы: оракул говорит «циклов нет», линейка
  «не применимо/пройдено»), расхождений 0;
- гейт стал строже без ущерба числам: `pass@1/scenario` 0.545 (как до прохода),
  `pass@1/loops_have_a_guard` = 1.0 при 2 применимых случаях — в наборе есть
  схемы с циклом, и они защищённые;
- 6 новых тестов на синтетике (цикл без развилки, развилка внутри, развилка
  снаружи, нет цикла → не применимо, согласие слоёв) — без них «строгий оракул»
  был бы неотличим от «оракул не смотрит туда, где должен».

Тестов **1146**. Отпечаток линейки объявил baseline несопоставимым (добавился
`loops_have_a_guard`), после `--write-baseline` гейт даёт код 0.

### Очередь после прохода

- Девять правил без зеркала сократились до восьми; оставшиеся восемь — мягкие
  (подпись, документация, тип, количество) или те, что не срабатывают ни на одном
  плане набора. Следующий шаг по этому же классу — не новое зеркало, а данные:
  кейс, где базовая схема держит четыре подписи подряд и где эталон осознанно
  хуже по `documentation`.
- Проверено и отвергнуто в этом же проходе: совет `guarded_cycles` нельзя
  дооснастить операндами. Единственная правка, снимающая нарушение, — вставить
  `exclusiveGateway` в дугу возврата и вторую ногу вывести из цикла; обе ноги
  такого шлюза обязаны нести критерий (`gateway_conditions`) или выход по
  умолчанию, а текст условия знает только автор процесса. Пакет, напечатанный
  дословно, снял бы `guarded_cycles` и тут же зарядил `gateway_conditions` — то
  есть недоделанный рецепт, а не improvement. Перепись поэтому права:
  «названо 0/8, операнд выбирает модель 8».
- `task_types` 62%, `RECIPES_IN_MESSAGE = 3`, live-прогон без `GIGACHAT_CREDENTIALS`.

## Ход двадцать четвёртого прохода: метрики снятия дефектов перестали быть пустыми

Очередь прошлых проходов упиралась в данные, и данные добавлены: пара фикстур
`purchase_approval.signoffs.bad.plan` (пять ручных подписей подряд в одной
дорожке, ни одной развилки по сумме) + `purchase_approval.signoffs.good.improve`
(эталонный пакет: развилка в цепочку, вторая нога мимо двух согласующих, условие
на обеих). Базовая схема — правдоподобный ответ модели по мотивам корпуса
(37 нарушителей `approval_chain` из 367), а не хвост метрики: остальные классы в
ней соблюдаются — два пула, шаги у обоих, обмен сообщениями, старт и финиш.

Что это изменило в числах харнесса (replay):

- `improve/defects_repaired` был 0.0 при 1 применимом случае → **0.500** при 2;
- `improve/business_repaired_share` был 0.0 → **0.500**;
- `improve/op_acceptance` 0.889, `improve/score_delta` +1.667,
  `improve/rules_regressed_share` 0, `package_revert_share` 0;
- цена честности: `pass@1/scenario` 0.545 → 0.500, `business/smells` 0.455 →
  0.500, `not_worse_than_etalon` 0.889 → 0.800. В набор вошла ещё одна заведомо
  битая схема — доли могут только упасть, и это обратное гейм-метрикам направление.

Правка пакета найдена не чтением, а прогоном сквозь продукт-гарант: две операции
(`add_gateway` + `connect`) снимали согласование и ветвление, но заряжали
`gateway_conditions_or_default` — нога шлюза к финдиректору оставалась без
критерия. Третья операция (`add_condition` по `F4`) законна именно потому, что
`add_gateway(after='A3', to='A4')` **переиспользует id существующего потока** для
ноги шлюза: этот id модель видит в инвентаре, а не выдумывает. Без третьей
опрации эталонный пакет был бы недоделанным рецептом из прошлых проходов.

Заперто таблицами: `EXPECTED_RAW_FAILS` (сырой провал структуры — только
`has_branching`), `EXPECTED_DISAGREEMENTS` (по этой схеме слои согласны: `{}`),
`NEW_INVARIANTS` пополнена `signoffs_need_a_gate` и `loops_have_a_guard`, а
`EXPECTED_NEW_FAILURES` называет единственного свидетеля класса в наборе — иначе
инвариант был бы проверкой, которая на eval-данных не звенит никогда.

Тестов **1153**; `--write-baseline` (состав набора изменился) →
`--fail-on-regression` код 0.

### Очередь после прохода

- Свидетель `loops_have_a_guard` в наборе отсутствует: фикстуру с безусловным
  циклом добавлять можно, но только если её правка не упирается в
  `gateway_conditions` (см. отрицательный результат прохода 23).
- `task_types` 62%, `RECIPES_IN_MESSAGE = 3`, live-прогон без ключей.

## Ход двадцать пятого прохода: второй свидетель — безусловный цикл, и он же вторая пара метрик

Пункт очереди прошёл: `purchase_approval.loop.bad.plan` (согласующий возвращает
заявку на проверку комплектности тем же потоком, каким отправляет её дальше —
развилки нет, критерия выхода нет) и `purchase_approval.loop.good.improve`.
Отрицательный результат прохода 23 здесь не помешал: он про *текст подсказки*,
которую пишет линейка (там операнда правда нет — условие знает только автор).
Эталонный пакет фикстуры автора имеет: базовая схема падала на
`loops_have_a_guard`, `no_blind_rework`, `has_branching` (балл 85), пакет из четырёх
операций снимает все три, регрессий нет, балл 96, гейт False → True.

Операнды легальны по той же причине, что и в проходе 24: `add_gateway(after='A3',
to='A2')` переиспользует id существующего потока возврата (F9) для ноги шлюза,
поэтому `add_condition(flow='F9', …)` адресует то, что модель видит в инвентаре;
прежняя прямая ветка F4 снимается и продолжается уже от шлюза с условием.

Числа после прогона (replay): `improve/defects_repaired` 0.5 → **0.667**,
`improve/business_repaired_share` 0.5 → **0.667**, `pass@1/loops_have_a_guard`
1.0 (2 применимых) → **0.667** (3 применимых) — класс теперь измеряется, а не
проверяется только синтетикой в юнитах. Честная цена та же:
`pass@1/scenario` 0.500 → 0.462, `business/smells` 0.500 → 0.538 (в набор вошла
ещё одна заведомо битая схема). `business/scorer_oracle_agreement` 1.0: по этой
схеме слои согласны (`EXPECTED_DISAGREEMENTS` пустой).

Таблицы пополнены: `EXPECTED_RAW_FAILS` (`has_branching` + `loops_have_a_guard`),
`EXPECTED_DISAGREEMENTS` (обе новые фикстуры — `{}`), `EXPECTED_NEW_FAILURES`
(`no_blind_rework`, `loops_have_a_guard` в порядке `NEW_INVARIANTS`).

Тестов **1160**; `--write-baseline` под новый состав набора →
`--fail-on-regression` код 0.

**Состояние среза:** проверен и лежит в рабочем дереве **незакоммиченным**
(`eval/fixtures/purchase_approval.loop.bad.plan.json`,
`eval/fixtures/purchase_approval.loop.good.improve.json`,
`tests/test_eval_harness.py`, `tests/test_eval_invariants.py`, `AGENTS.md`,
этот файл, `eval/baselines/current.json`). `main` на момент записи совпадает с
`origin/main` (последний пуш — `a6a1591`, пара про согласования). Коммит и пуш
этого среза сделаны по первому слову пользователя; политика автоматического
режима блокирует их в continuation-туре без свежей явной просьбы.

**Как лендить этот срез (одной командой, проверено, что оно нужно именно ему):**

```bash
git add eval/fixtures/purchase_approval.loop.bad.plan.json \
        eval/fixtures/purchase_approval.loop.good.improve.json \
        tests/test_eval_harness.py tests/test_eval_invariants.py \
        AGENTS.md docs/plans/improve-contour-effectiveness.md \
        eval/baselines/current.json
git commit -m "feat(eval): второй парный кейс — безусловный цикл возврата тоже измеряется"
git push origin main
```

Заголовок по делу: база падает на `loops_have_a_guard`, `no_blind_rework`,
`has_branching` (балл 85), пакет из четырёх операций снимает все три, регрессий
нет, балл 96; `improve/defects_repaired` и `business_repaired_share` 0.5 → 0.667;
`pass@1/scenario` 0.500 → 0.462 как честная цена ещё одной заведомо битой схемы в
наборе. Основание для коммита: `pytest tests/ -q` → 1160 passed,
`eval.run --mode replay --fail-on-regression` → код 0.

### Очередь после прохода

- `task_types` 62% — потолок по операнду (тип называет модель).
- `RECIPES_IN_MESSAGE = 3` — хвост списка участников в промпт не попадает.
- Live-прогон (`настоящий op_acceptance` на ответе модели) без `GIGACHAT_CREDENTIALS`
  невозможен: replay переигрывает записанные пакеты, а не мышление модели.

---

## Ход двадцать шестого прохода: живой прогон стал возможен, и он вскрыл противоречие в самой линейке улучшения

Ключи лежат в `.env`, а харнесс читал только окружение: `--mode live` из чистой
оболочки падал с кодом 2. Исправлено проводкой (`eval/run.py`): для `live`
недостающие ключи дочитываются из `.env` (`override=False` — переменная
окружения важнее файла), `replay` файл не трогает. Три теста: live подхватил
ключ из временного файла; явная переменная не перезаписалась файлом; replay не
позвал загрузчик. Существующий тест «live без ключей честно падает» обязан был
остаться честным после правки — в нём загрузка глушится, иначе проверка
отсутствия ключей зависела бы от того, что лежит у разработчика в рабочей
копии.

### Прогон #55 (`--mode live --repeat 2`, `GigaChat-2`, 16 кейсов, отказов транспорта 0)

Полнота: `generation_error_share` 0 — throttle-окна не было, все 16 кейсов
собраны, знаменатели честные. До этого живой прогон не аттестовался с #54, и
#54 был throttled.

Гейт корректности: `pass@1/scenario` **0** (16 из 16 проваливают хотя бы один
инвариант), `pass@1/structure` 0, `structure_xml_agreement` 1. Ниже планки 0.8
оказались три корректностных инварианта: `has_branching` **0.438**,
`expected_participants` **0.500**, `message_flow_ends` **0.750**. Выше:
`roles_as_lanes` 0.750 — тоже ниже 0.8, четвёртое имя.
Бизнес-слой (вне гейта): `waits_have_sla` 0.200, `no_overloaded_lane` 0.692,
`signoffs_need_a_gate` 0.857, `business/smells` 0.812,
`business/scorer_oracle_agreement` **0.988**, `business/not_worse_than_etalon`
0.333. Балл 93.06 (мин 84, cv 0.051, на кейс 0.017), задержка 13.9 с,
повторов 2.875 на задачу.

Кто породил 42 провалов: «модель:повтор не тронул это нарушение» 15 (9 из них
при том, что план-гейт про нарушение знал), «модель:переспрос» 3 + 6 «переспрос,
о котором план-гейт молчал», починка — 13 (события пула 4, достижимость 3,
вставка развилок 1, закрытие маршрутов 1), вопрос о принадлежности 3, первый
ответ 3. То есть 12 из 42 — контур знал про нарушение и всё равно его не закрыл,
а 6 из 42 — контур про него молчал; последнее и есть слепое пятно гейта.

### Противоречие, которое вскрыл прогон: метрики улучшения измеряют не то в live

`improve/score_delta` **−3.25**, `defects_introduced` 0.125,
`rules_regressed_share` 0.125, `defects_repaired` 0.292,
`business_repaired_share` 0.250, `op_acceptance` 0.766.

Читатель делает из этого вывод «пакет портит схему», и он неверен: в live
фикстура — не ответ модели, а слот прогона (`harness.py:1673-1681`, парковка
пакета к первой собранной схеме — `harness.py:1690-1697`). Промпты парных
фикстур описывает дефект *своей* базовой фикстуры («согласующий возвращает
заявку тем же потоком…», «мелкие закупки вязнут в согласовании…»), а базой в
live лежит живая схема `purchase_approval`, где этого дефекта нет: в том же
прогоне `loops_have_a_guard` и `no_blind_rework` прошли 1/1. Модель честно
вставляет развилку туда, где её просили, и получает `gateway_conditions_or_default`
и `gateway_split_join` в колонке «сломано пакетом» — при 8 пулах и 29 элементах
живой схемы. Отказы того же сорта: `plan/add_condition: 'F6' не sequence-поток`
— id из фикстуры в живой схеме принадлежит потоку-сообщению.

Это ровно тот класс, о котором говорит цель: метрика противоречива, потому что
смешивает качество пакета с применимостью претензии. Правка (не ослабление, а
сопоставление): у improve-фикстуры появляется поле `targets` — список
инвариантов, дефект которых она описывает; в live кейс считается только если
базовая схема действительно проваливает хотя бы один из `targets`, иначе
пишется `improve/advice_not_applicable_share` с явной причиной «претензия не про
эту схему». Так `score_delta` начнёт мерить пакет, а не выдуманный дефект, и
число неприменимых кейсов перестанет прятаться внутри доли отремонтированного.

### Очередь после прохода

- Применимость live-пакета (`targets` + `advice_not_applicable_share`) — снятый
  выше артефакт, самая дешёвая честность в линейке улучшения.
- `has_branching` 0.438 — владелец «повтор не тронул»: нога развилки без
  условия правится (`da7aa94`), но развилки как узла модель не добавляет.
- `expected_participants` 0.500 — именует не словами описания; код подставить
  имя не имеет права.
- `message_flow_ends` 0.750 — конец потока-сообщения у шлюза: совет
  `cross_pool_flow` легальную форму называет, модель её не берёт.
- `waits_have_sla` 0.200 — бизнес-слой вне гейта, эталон держит тот же долг.
- 12 из 42 провалов при знавшем план-гейте: переспрос получает требование, но
  не может его исполнить (состав заморожен) — резерв того же класса, что и
  `deferred_actor_gaps`.

---

## Ход двадцать седьмого прохода: противоречие live-линейки улучшения закрыто, а живая выборка — нет

Правка по снятому в проходе 26 диагнозу. Ключевое слово — *применимость претензии*.

### Что сделано

1. **`targets` у improve-фикстуры** — список инвариантов, дефект которых описывает
   текст претензии. Значения взяты не из головы, а с прогона: у пары про цикл это
   `loops_have_a_guard` + `no_blind_rework`, у пары про согласование —
   `signoffs_need_a_gate`, у живого пакета склада — `waits_have_sla`, у
   `production_incident.good.improve` — пустой список (пакет просит улучшение,
   нарушения в базе нет: `fails_before` пуст).
2. **Фильтр до обращения к модели** (`_mark_off_target`): ни один `targets` не
   проваливается — кейс помечается `off_target`, в модель не идёт, и выпадает из
   знаменателя всех метрик улучшения (гейт в `build_improvement_suite` оборачивает
   каждую функцию метрики; исключение — сама применимость). Отчёт печатает
   отдельную строку «правка не запрашивалась» с обоими списками, иначе кейс без
   пакета читался бы как пакет, который ничего не смог.
3. **Новая метрика `improve/advice_off_target_share`** (LOWER, добавлена в
   `LOWER_IS_BETTER`, чтобы fallback не расходился с объявленным направлением).
4. **Кейс «претензия по факту»** (`_advice_fixtures` + `ADVICE_PROMPT`): к каждой
   собранной живой схеме добавляется один improve-кейс, чьи `targets` — фактические
   провалы этой схемы, а текст требования статичен и не содержит ни id, ни имён
   участников, ни названий правил. Подсказки скоринга в промпт дописывает сам
   оркестратор (`core/llm_improve.py:1013`), так что харнесс модели не помогает.
   Без этого шага фильтр превратил бы live-выборку улучшения в ноль: живая схема
   редко болеет ровно тем, чем больна фикстура.
5. **Рэтчет набора**: каждая improve-фикстура обязана объявить `targets`, а
   объявленное обязано пересекаться с провалами её же базовой схемы.

### Что измерено

Replay после правки: `advice_off_target_share` 0 (n=3), `score_delta` 4.0,
`defects_repaired` 0.667 — то есть фикстурные пары остались измеряемыми, ничего не
спряталось. Baseline пересобран, гейт `--fail-on-regression` даёт 0, харнесс
честно сообщает «наборы метрик разошлись: improve/advice_off_target_share», пока
ключ не записан.

Live, отборочный прогон на двух сценах:
- `purchase_approval --repeat 1`: обе фикстурные претензии ушли out-of-target
  (живая схема проваливает `roles_as_lanes` и `waits_have_sla`), вызовов модели на
  улучшение — ноль, `advice_off_target_share` 1.0, метрики качества — «н/д», `n` 0.
  Ровно то поведение, которого не хватало: раньше здесь же модель вставляла
  развилку в процесс без цикла и получала «сломано пакетом».
- `warehouse_delivery --repeat 1`: фикстурная претензия (`waits_have_sla`) тоже не
  про эту схему, а советный кейс собрался и дал живую выборку — 4 применённые
  операции из 9, повтор закрыл 1 отказ, `op_acceptance` 0.444,
  `defects_repaired` 0.0 при двух дефектах базы (`expected_participants`,
  `min_steps`), балл 100 → 100. То есть первая честная пара чисел: пакет умеет
  двигать схему и не умеет убирать то, о его чем просили.

Прогон #56 (`--mode live --repeat 2`, полный набор) запущен параллельно с этим
разделом; его цифры будут дописаны здесь после завершения.

### Чего эта правка НЕ делает

Не ослабляет оракул: решение о применимости принимается по тому же независимому
прогоду, которым потом мерится `defects_repaired`, а не по тексту фикстуры. Не
добавляет обращений к модели в фикстурные слоты — только убирает лишние. Не
превращает `advice`-кейсы в «метрику на тестовых данных»: схема живая, промпт
статичный, few-shot образцы промпта генерации в нём не участвуют, provenance-аудит
прогона это подтверждает.

### Очередь после прохода

- Прочитать #56: `advice_off_target_share` по всем сценам, `defects_repaired` и
  `op_acceptance` на живых пакетах, `pass@1/*` против #55 (тот же пул моделей,
  сравнимо).
- `op_acceptance` 0.444 live против 0.800 replay — расхождение двух режимов на
  одном контуре: в replay пакет записан руками, в live модель выдумывает operands.
  Это следующий владелец отказов, и он лечится словарём операций, а не оракулом.
- `has_branching` 0.438 и `expected_participants` 0.500 (#55) — без изменений,
  владелец «повтор не тронул».

### Прогон #56 (`--mode live --repeat 2`, 16 генераций + 20 кейсов улучшения, отказов транспорта 0)

Снят тем же контуром, что и #55, — сравнение корректно (тот же пул моделей, та же
линейка, `ruler` не менялся; новая метрика только добавлена).

| метрика | #55 | #56 |
|---|---|---|
| `improve/score_delta` | −3.25 (n 8) | **+3.632** (n 19) |
| `improve/defects_repaired` | 0.292 (n 8) | 0.206 (n 19) |
| `improve/business_repaired_share` | 0.250 | 0.300 (n 15) |
| `improve/op_acceptance` | 0.766 (n 8) | 0.570 (n 19) |
| `improve/defects_introduced` | 0.125 | 0.053 |
| `improve/rules_regressed_share` | 0.125 | 0.158 |
| `improve/advice_off_target_share` | — | 0.182 (n 22) |
| `pass@1/scenario` | 0 (n 16) | 0.062 (n 16) |
| `score` | 93.06 | 91.63 |
| `business/smells` | 0.812 | 0.938 |
| `business/not_worse_than_etalon` | 0.333 | 0.417 |
| `business/scorer_oracle_agreement` | 0.988 | 0.938 |
| `improve/base_advice_drift` | 0 | 0.263 |

Чтение. Положительная `score_delta` при удвоенной выборке — и есть то, ради чего
правилась метрика: раньше −3.25 был платой за правки, которые никто не просил.
`op_acceptance` и `defects_repaired` при этом честнее упали: в старую выборку
попадали пакеты, где модель «чинила» то, что и так работало, и это выглядело
успехом. 4 из 22 слотов помечены вне цели и не стоили ни одного обращения —
раньше на них уходило 4 живых пакета.

Что осталось и стало видно: `improve/base_advice_drift` 0.263 и
`scorer_oracle_agreement` 0.938 (было 0.988) — два слоя разошлись на базовых
схемах живого прогона; это следующий кандидат на разбор, потому что по
расходящемуся правилу совет уходит в промпт вслепую. На стороне генерации ничего
не изменилось: `pass@1/scenario` 0.062, владельцы провалов те же (ветвление и
именование участников).

---

## Ход двадцать восьмого прохода: самый частый живой отказ получил исполнимую подсказку

Перепись пропусков аплайера по живым отчётам (61 файл, 572 строки отказа) сначала
выглядела как «модель выдумывает участников»: 109 строк «пул не определён», из них
78 — с названным `participant`, и 69 из них ровно одним значением, «руководитель
смены». Разбор по датам показал другое: этот класс уже вылечен подсказкой,
перечисляющей имеющиеся пулы, — в прогонах 25–26 сентября таких строк шесть, а на
первом месте «дорожка принадлежит другому пулу» (12) и «такой поток уже
существует» (14; последний оставлен намеренно — это законный no-op).

Отказ по чужой дорожке был самым молчаливым: текст «указывайте lane дорожкой того
же пула» не называл ни владельца дорожки, ни того, что модель может ответить, —
повтор приносил ровно ту же операцию. Теперь `_lane_pool_skip` говорит оба пула,
id спорной дорожки, имена законных дорожек пула элемента, а если их нет — форму
`add_lane(id=…, name=…, participant="<имя пула>")`. Пул при этом не подменяется:
операция по-прежнему отвергается, исполнимой стала подсказка. Правило то же, что
для советов скоринга: «совет без операндов модель читает как "придумай id"».

Замер: три новых контракта в `tests/test_bpmn_edits.py` (красные до правки —
`hint` был «переносить элемент можно только в дорожку своего процесса»), 1173
теста, replay-гейт 0. Эффект на `op_acceptance` будет виден в #57: он может
подняться, а может и нет — подсказка помогает только тогда, когда модель слышит
повтор; измеряется он той же переписью отказов по отчёту прогона.

---

## Ход двадцать девятого прохода: недостижимые метрики сняты, сводная доля введена, субъективное отдано человеку

Постановка: «метрики, которые тупо не достигаются до нормальных значений, —
объяснить и убрать; сделать унифицированную метрику, которую можно добить до 0.8;
субъективное оценивать руками».

Что измерено перед решением. Доля применимых гейт-проверок, пройденных одной
схемой, пересчитана по обоим живым отчётам тем же `deciding_checks`, каким меряет
харнесс: #56 — 0.863 (медиана 0.90, 15 кейсов из 16 ≥0.8), #57 — 0.814 (9 из 16).
Конъюнкция тех же проверок в тех же прогонах: 0.062 и 0. Разница не в строгости
меры, а в её форме: при ~17 независимых условиях произведение обязано схлопнуться
к нулю, пока не закрыт каждый класс сразу. Это и был ответ на «почему не
достигается»: `pass@1/scenario`, `pass@1/structure` и `improve/pass@1` не меряли
контур, они меряли геометрию произведения.

Что снято и чем заменено. Три конъюнкционные строки убраны из таблицы, ничего
не потерявшись из данных: сводный исход кейса живёт в `scenario_pass`/`pass_after`,
в имени файла `--dump-schemes` и в атрибуции. На их месте —
`quality/scheme_checks_share` (планка ≥0.8: replay 0.929, live 0.863/0.814),
`quality/structure_checks_share` (тот же вопрос к плану, а не к транспорту XML) и
пара `improve/base_checks_share` → `improve/scheme_checks_share` (в replay 0.910 →
0.982: вот вклад пакета в нотацию, очищенный от наследования провалов генерации).
Бизнес-слой сведён в `quality/business_checks_share` (replay 0.833), но планкой не
объявлен: его потолок задан набором, и узкое место держит сам эталон
(`EXPECTED_ETALON_BUSINESS_DEBT`), — ослаблять оракул ради доли прохождения
нельзя, поэтому вместо планки там `business/not_worse_than_etalon`.

Удаление метрики не должно выглядеть как «сверили всё и регрессий нет» — за это
отвечает `unverified_metrics`: свежий прогон против слепка старше правки печатает
«не сверялись с baseline (наборы метрик разошлись): …» и по удалённым ключам, и по
новым. Проверено на записи baseline: строка назвала все три снятых имени.

Субъективное — руками, без метрики. Раздел «РУЧНАЯ ОЦЕНКА» несёт четыре вопроса
читателю (имя шага читается как действие; в схеме нет шага, которого нет в
описании; участники названы словами описания; схема открывается в bpmn-js и
маршрут виден без разбора id) и адрес схемы — только те, что автоматика уже
пропустила, потому что у брака владелец назван в атрибуции и смотреть его глазами
бессмысленно. Числа в разделе нет намеренно: выдуманная мера превратила бы ручной
вердикт в ещё одну самоподтверждающуюся метрику.

Вторая снятая несостыковка — порог монополии дорожки. Оракул держал 75% на все
пулы с двумя и более дорожками, скоринг при трёх и более берёт 60%, и на корпусе
это 18 схем, где продукт ругался, а независимая приёмка молчала; в живом прогоне
#56 из-за этого `business/scorer_oracle_agreement` = 0.938. Оракул переведён на те
же два порога и печатает применённый в сообщении, знаменатель остался своим (работы
всего пула, а не только размеченных дорожек), `core` по-прежнему не импортируется —
константа продублирована и названа ценой. Итог: `DOCUMENTED_DIVERGENCES` — одна
пара (`rework_loop`), а в #57 согласование двух линеек = 1.0 при 16 кейсах.

Прогон #57 (live, 2 повтора, 16 сцен + 24 пакета) прочитан целиком:
`business/scorer_oracle_agreement` 0.938 → 1.0, `defects_repaired` 0.206 → 0.315,
`waits_have_sla` 0 → 0.25, балл 91.6 → 92.6. Против: `op_acceptance` 0.570 → 0.458,
`repeat_rejection_share` 0.040 → 0.155, `error_share` 0.05 → 0.10,
`has_branching` 0.688 → 0.438, `expected_participants` 0.438 → 0.313. То есть
подсказка про чужую дорожку не подняла приёмку: отказ по-прежнему на втором месте
(20 строк вместо 11), а на первое вышла «источник или цель не найдены» — 26 строк
с подсказкой «используйте существующие id из инвентаря». Это ровно тот класс,
который уже чинился трижды: приказ угадать вместо названных операндов. Следующий
ход — `connect` обязан назвать, чего не хватает (id источника/цели из инвентаря
этого пула, а при близком совпадении — тот самый id), и проверить это контрактом
`TestAdviceIsExecutable` по живой фикстуре.

Гигиена: проверка склейки кириллицы с латиницей знала только строчные буквы, и
`Рatchet` в начале предложения проходил мимо. Класс расширен, самопроверка
паттерна закреплена тестом, на всём репозитории расширение дало ровно одно
срабатывание — тот же `Рatchet`. Итог прохода: 1177 тестов, replay-гейт 0,
baseline пересобран под новый набор метрик.

---

## Ход тридцатого прохода: отказ дуги называет несостоявшееся создание опоры

Разбор верхнего живого отказа #57 («источник или цель не найдены», 26 строк) по
полям пакета, а не по тексту подсказки, дал три разных владельца. Каскад — 13
строк: опора ссылки есть в пакете, но её создание этот же пакет отклонил
(`new_report_manager` — «пул не определён», `new_sla_timer_1` — откат без ветки
обработки). Чистая выдумка — 5 строк, где ни один из двух id пакетом не создавался.
Порядок — 4 строки, где оба узла пакетом созданы (это уже закрыто отложенными
проходами `MAX_PACKAGE_PASSES`), и 4 строки смешанного следа, где один operand
создан, а второй в пакете не значится.

Причина метрики, а не текст: «используйте существующие id из инвентаря» при
каскаде отправляет повтор латать дугу там, где не создан узел, и раунд уходит
впустую — он и дал `repeat_rejection_share` 0.155. Теперь аплайер ведёт
`failed_creations` (отказ создания, отложенная и так и не созданная операция,
откат узла без маршрута) и `_explain_failed_dependencies` дописывает в подсказку
ту строку, которая действительно блокирует правку: «опора не создана этим же
пакетом: 'new_A9' — пул не определён — почините сначала создание, дуга применится
после», с прежним текстом следом. Тот же признак применяется не только к `connect`:
подсказка чинится у любой правки, чей operand назван в отказе создания.

Замер: три контракта в `tests/test_bpmn_edits.py`, главный из них
`test_connect_refusal_names_the_creation_that_failed` краснел до правки — подсказка
была «используйте существующие id из инвентаря»; 1180 тестов, replay-гейт без
регрессий. Эффект на `op_acceptance`
читается по живому прогону: ожидается падение «источник или цель не найдены» и
рост `improve/retry_gain`, потому что корректирующий повтор теперь получает
устранимую причину.

---

## Ход тридцать первого прохода: живая приёмка пакета выросла, а ветвление перестал ломать контур

**#58 (`--mode live --repeat 2`, 16 сцен + 24 пакета, транспорт живой, отказов
сборки 0).** Подсказка про несостоявшееся создание опоры из прошлого хода
измерена против двух предыдущих прогонов:

| метрика | #56 | #57 | #58 |
|---|---|---|---|
| `improve/op_acceptance` | 0.570 | 0.458 | **0.704** |
| `improve/noop_share` | 0.012 | 0.038 | **0** |
| строки «источник или цель не найдены» | 5 | 26 | **8** |
| `improve/score_delta` | 3.632 | 0.889 | 2.062 |
| `pass@1/message_flow_ends` | 0.733 | 0.625 | 0.857 |
| `quality/scheme_checks_share` (сводка гейта) | 0.863 | 0.814 | 0.846 |
| `improve/error_share` | 0.05 | 0.10 | 0.20 |
| `improve/defects_repaired` | 0.206 | 0.315 | 0.188 |

Приёмка пакета выросла именно там, где её ломал каскад, и перестала быть
единичным числом: строка «источник или цель не найдены» упала с 26 до 8. Цена
честности — `error_share` 0.20 (4 пакета из 20 не применились целиком) и
`defects_repaired` 0.188: выборка перестала быть красивой, когда в неё вошли
пакеты, которые раньше отбраковывались молча. Верхние оставшиеся отказы: «такой
поток уже существует» (6, законный no-op), «цель исхода не найдена/в другом пуле»
(6), `merge_participants` с `source='L4'` и `'Lane_2'` (4 — модель назвала
дорожку вместо пула, подсказка «используйте имена участников из инвентаря»
операнда не даёт), `add_timer` как неизвестная операция (2 — словарь `OP_SPEC`
против имени, которое модель берёт из текста рецепта).

**Второй класс, найденный в этом же прогоне: ветвление убивает сам контур.**
Перепись 9 живых провалов `has_branching` по трейсу: у всех 9 стоит шаг починки
«понижение одновыходных шлюзов», у 6 до него — перенос шага в пул по имени
(`_claim_steps_by_name`), и именно он превращает ногу шлюза в поток-сообщение:
у шлюза остаётся одна легальная нога, и починка снимает его. Схема, где «если
машин нет — запросить у перевозчика» нарисована шлюзом, выходит без развилки, а
винили модель.

Снято тремя правками, каждая с измеренной причиной:

* `_gateway_branch_targets` + запрет переноса: шаг, в который идёт нога
  действительно раздваивающего шлюза, в чужой пул не переносится, в отчёте
  появляется строка «нога развилки» (пустой пул контрагента остаётся ждать
  узкого вопроса — это нарушение `pool_has_steps`, а не повод ломать маршрут).
* новая проверка план-гейта: нога развилки, которую модель сама повела в чужой пул,
  называется потоком и пулом (`ведёт в чужой пул`, приоритет 1, класс «развилка»):
  правка — шаг своего пула, отправляющий сообщение, а не удаление развилки.
* атрибуция: провал инвариантов ветвления после понижения шлюза принадлежит
  починке (`починка:понижение одновыходных шлюзов`), а не модели; если о
  ветвлении уже жаловался план-гейт, понижение другого узла вины не меняет.

Замер: 3 контракта в `tests/test_bpmn_generator.py` (в том числе негативный —
ветка внутри своего пула новую претензию не получает) и 3 в
`tests/test_eval_attribution.py`; 1186 тестов; replay-гейт 0, сводка гейта
0.929, `improve/base_checks_share` 0.910 → `improve/scheme_checks_share` 0.982.

---

## Ход тридцать второго прохода: участник, названный в описании, перестаёт исчезать со схемы

**#59 (`--mode live --repeat 2`, 16 сцен + 24 пакета, транспорт живой, отказов
сборки 0).** Прогон мерил три правки прошлого хода — подсказку об операнде
дорожки, алиас `add_timer` и сохранение ветвления:

| метрика | #58 | #59 |
|---|---|---|
| `pass@1/expected_participants` | 0.375 | **0.75** |
| `pass@1/has_branching` | 0.562 | 0.625 |
| `quality/scheme_checks_share` | 0.846 | **0.876** |
| `improve/scheme_checks_share` | 0.844 | **0.883** |
| `improve/defects_repaired` | 0.188 | 0.238 |
| `improve/op_acceptance` | 0.704 | 0.652 |
| `improve/error_share` | 0.20 | 0.30 |
| `improve/retry_gain` | 0.18 | 0 |

Числа живого прогона шумят на одном и том же материале: `op_acceptance` и
`error_share` в #59 ухудшились там, где правки прошлого хода не работали, а
`retry_gain` упал до нуля, потому что корректирующий повтор в этом прогоне
вызывался реже (пакеты с каскадом перестали отваливаться пачками). Отдельный
live-baseline как раз для этого и нужен: сверка с replay-базлайном пропускается,
и остаётся только глазное сравнение прогонов между собой.

**Оставшиеся два провала `expected_participants` разложились на два механизма, и
оба — вина контура, а не модели.**

*Починка удаляла названного участника.* `_drop_vacant_pools` убивает пул без
единого шага, не зная, откуда имя: «компания-перевозчик» из описания
`vehicle_reservation` и «WMS» из #58 уходили со схемы вместе с событиями, хотя
`plan_gaps` строкой выше прямо запрещал это делать («Участник назван в описании,
поэтому убирать его нельзя»). Пять строк из десяти провалов #58 — этот класс.
Выбор в пользу оставленного пула: нарушение `pool_has_steps` чинится пакетом,
у которого есть операнды (`participant="WMS"` из инвентаря), а удалённый пул из
инвентаря исчезает, и правке остаётся только выдумывать id. Терпимость имени та
же, что у оракула (`_mentioned`), иначе починка расходилась бы с приёмкой:
«расчёт с перевозчиком» называет пул «компания-перевозчик».

*План-гейт молчал об участнике в начале фразы.* `_proper_names` отбрасывает
заглавное слово после точки намеренно, иначе каждый «Далее» и «Если» просил бы
свой пул, — но в русских описаниях процессов действующее лицо чаще всего и стоит
в начале фразы. Добавлен признак сказуемого: слово в начале фразы становится
кандидатом, если через него-другое идёт глагол третьего лица. «Клиент
подтверждает дату погрузки» — участник, «Резервирование транспорта под заявку» и
«заявка отклоняется» — нет (отглагольное существительное и пассив). Проверка на
восьми текстах набора: добавленные кандидаты — «Оператор», «Система»,
«Кладовщик», «Служба», «Экспедитор», «Инициатор», «Покупатель», «Бухгалтерия»,
«Дежурный», «Скоринговый», «Диспетчер», «Клиент»; ложных среди них нет, а
`_proper_names` на тех же текстах не давал ни одного.

Контракт, который правка поправила, был заперт тестом
`test_start_of_a_phrase_is_not_taken_for_a_participant` — он охранял именно
ограничение. Переписан в `test_start_of_a_phrase_needs_a_predicate_to_be_a_participant`
и держит теперь то, ради чего ограничение вводилось: «Далее» и «Получатель»
посреди фразы кандидатом не становятся. Второй затронутый тест,
`test_rescue_refuses_to_empty_the_donor_pool`, проверяет отказ переноса (шаг
остаётся в своём пуле) и больше не требует удаления названного пула: это разные
решения, и второе противоречит `plan_gaps`.

Тесты: 4 в `TestPhraseStartActor` (признак сказуемого, отрицание на нарицательном
и на закрытой дорожке), 1 в `TestVacantPools` (пустой пул названного участника
доживает до приёмки) — 1194 теста, replay-гейт 0, сводка гейта 0.929,
`pass@1/expected_participants` 0.846, `pass@1/pool_has_steps` 1.0.
