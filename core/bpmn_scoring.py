import xml.etree.ElementTree as ET
from typing import Dict, List
from collections import defaultdict
import copy
from xml.sax.saxutils import escape

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"

ET.register_namespace("bpmn", BPMN_NS)
ET.register_namespace("bpmndi", BPMNDI_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("di", DI_NS)

class BPMNScorer:
    def __init__(self):
        self.namespace = {'bpmn': BPMN_NS}
        self.rules = {
            'start_event': {'weight': 10, 'message': 'Количество стартовых событий должно соответствовать числу участников'},
            'end_event': {'weight': 10, 'message': 'Должно быть хотя бы одно конечное событие'},
            'gateway_conditions': {'weight': 15, 'message': 'Эксклюзивные шлюзы должны иметь условия на всех исходящих потоках'},
            'sequence_flows': {'weight': 10, 'message': 'Все элементы должны быть соединены последовательностями'},
            'direction': {'weight': 8, 'message': 'Процесс должен быть направлен слева направо'},
            'naming': {'weight': 10, 'message': 'Все элементы должны иметь осмысленные названия (не короче 3 символов)'},
            'no_loops': {'weight': 10, 'message': 'Схема не должна содержать бесконечных циклов'},
            'element_count': {'weight': 8, 'message': 'Схема не должна быть перегружена элементами (>50)'},
            'no_isolated': {'weight': 8, 'message': 'Схема не должна содержать изолированных элементов'},
            'task_types': {'weight': 8, 'message': 'Схема должна содержать разнообразные типы задач'},
            'pool_lanes': {'weight': 8, 'message': 'Схема должна использовать пулы или дорожки с элементами'},
            'event_types': {'weight': 8, 'message': 'Схема должна включать промежуточные события (таймеры, сообщения)'},
            'documentation': {'weight': 7, 'message': 'Элементы должны содержать документацию'},
        }

    def evaluate(self, bpmn_xml: str) -> Dict:
        try:
            root = ET.fromstring(bpmn_xml)
            score = 0
            recommendations = []
            errors = {}
            optimized_root = copy.deepcopy(root)

            # Кэширование элементов
            elements_cache = {
                'start_events': root.findall('.//bpmn:startEvent', self.namespace),
                'end_events': root.findall('.//bpmn:endEvent', self.namespace),
                'exclusive_gateways': root.findall('.//bpmn:exclusiveGateway', self.namespace),
                'parallel_gateways': root.findall('.//bpmn:parallelGateway', self.namespace),
                'tasks': root.findall('.//bpmn:task', self.namespace),
                'user_tasks': root.findall('.//bpmn:userTask', self.namespace),
                'service_tasks': root.findall('.//bpmn:serviceTask', self.namespace),
                'sequence_flows': root.findall('.//bpmn:sequenceFlow', self.namespace),
                'collaboration': root.find('.//bpmn:collaboration', self.namespace),
                'lane_sets': root.findall('.//bpmn:laneSet', self.namespace),
                'intermediate_events': root.findall('.//bpmn:intermediateCatchEvent', self.namespace) +
                                     root.findall('.//bpmn:intermediateThrowEvent', self.namespace),
            }

            # Проверка стартовых событий с учетом участников
            participant_count = 1
            if elements_cache['collaboration'] is not None:
                participants = elements_cache['collaboration'].findall('.//bpmn:participant', self.namespace)
                participant_count = len(participants) if participants else 1
            if len(elements_cache['start_events']) == participant_count:
                score += self.rules['start_event']['weight']
                errors['start_event'] = True
            else:
                recommendations.append(f"{self.rules['start_event']['message']}: ожидается {participant_count} стартовых событий, найдено {len(elements_cache['start_events'])}")
                errors['start_event'] = False

            # Проверка конечных событий
            if len(elements_cache['end_events']) >= 1:
                score += self.rules['end_event']['weight']
                errors['end_event'] = True
            else:
                recommendations.append(self.rules['end_event']['message'])
                errors['end_event'] = False

            # Проверка условий у эксклюзивных шлюзов
            all_gateways_valid = True
            for gateway in elements_cache['exclusive_gateways']:
                outgoing = gateway.findall('bpmn:outgoing', self.namespace)
                if len(outgoing) < 2:
                    all_gateways_valid = False
                    break
                flows = [root.find(f".//*[@id='{out.text}']", self.namespace) for out in outgoing]
                if not all(flow is not None and flow.find('bpmn:conditionExpression', self.namespace) is not None for flow in flows):
                    all_gateways_valid = False
                    break
            if all_gateways_valid and elements_cache['exclusive_gateways']:
                score += self.rules['gateway_conditions']['weight']
                errors['gateway_conditions'] = True
            elif elements_cache['exclusive_gateways']:
                recommendations.append(self.rules['gateway_conditions']['message'])
                errors['gateway_conditions'] = False

            # Проверка связей между элементами
            all_tasks = elements_cache['tasks'] + elements_cache['user_tasks'] + elements_cache['service_tasks']
            all_tasks_connected = True
            for task in all_tasks:
                incoming = task.findall('bpmn:incoming', self.namespace)
                outgoing = task.findall('bpmn:outgoing', self.namespace)
                if len(incoming) == 0 or len(outgoing) == 0:
                    all_tasks_connected = False
                    break
            if all_tasks_connected and all_tasks:
                score += self.rules['sequence_flows']['weight']
                errors['sequence_flows'] = True
            elif all_tasks:
                recommendations.append(self.rules['sequence_flows']['message'])
                errors['sequence_flows'] = False

            # Проверка направления процесса
            process = root.find('.//bpmn:process', self.namespace)
            if process is not None:
                score += self.rules['direction']['weight']
                errors['direction'] = True
            else:
                recommendations.append(self.rules['direction']['message'])
                errors['direction'] = False

            # Проверка именования элементов
            named_elements = root.findall('.//*[@name]', self.namespace)
            total_elements = (len(all_tasks) + len(elements_cache['exclusive_gateways']) +
                            len(elements_cache['parallel_gateways']) + len(elements_cache['start_events']) +
                            len(elements_cache['end_events']))
            valid_names = sum(1 for elem in named_elements if elem.get('name', '').strip() and len(elem.get('name', '')) >= 3)
            if total_elements > 0 and (valid_names / total_elements >= 0.9 if total_elements else True):
                score += self.rules['naming']['weight']
                errors['naming'] = True
            else:
                recommendations.append(f"{self.rules['naming']['message']}: только {valid_names} из {total_elements} элементов имеют корректные названия")
                errors['naming'] = False

            # Проверка на циклы
            graph = defaultdict(list)
            for flow in elements_cache['sequence_flows']:
                source = flow.get('sourceRef')
                target = flow.get('targetRef')
                if source and target:
                    graph[source].append(target)

            def has_cycle(node: str, visited: set, stack: set) -> bool:
                visited.add(node)
                stack.add(node)
                for neighbor in graph.get(node, []):
                    if neighbor not in visited:
                        if has_cycle(neighbor, visited.copy(), stack.copy()):
                            return True
                    elif neighbor in stack:
                        return True
                stack.remove(node)
                return False

            has_loops = False
            visited = set()
            for node in graph:
                if node not in visited:
                    if has_cycle(node, visited, set()):
                        has_loops = True
                        break
            if not has_loops:
                score += self.rules['no_loops']['weight']
                errors['no_loops'] = True
            else:
                recommendations.append(self.rules['no_loops']['message'])
                errors['no_loops'] = False

            # Проверка на количество элементов
            total_elements_count = total_elements
            if total_elements_count <= 50:
                score += self.rules['element_count']['weight']
                errors['element_count'] = True
            else:
                recommendations.append(f"{self.rules['element_count']['message']}: найдено {total_elements_count} элементов")
                errors['element_count'] = False

            # Проверка на изолированные элементы
            all_elements = set()
            connected_elements = set()
            for element in (all_tasks + elements_cache['exclusive_gateways'] +
                           elements_cache['parallel_gateways'] + elements_cache['start_events'] +
                           elements_cache['end_events']):
                element_id = element.get('id')
                if element_id:
                    all_elements.add(element_id)
                    incoming = element.findall('bpmn:incoming', self.namespace)
                    outgoing = element.findall('bpmn:outgoing', self.namespace)
                    if len(incoming) > 0 or len(outgoing) > 0:
                        connected_elements.add(element_id)
            if not (all_elements - connected_elements):
                score += self.rules['no_isolated']['weight']
                errors['no_isolated'] = True
            else:
                recommendations.append(self.rules['no_isolated']['message'])
                errors['no_isolated'] = False

            # Проверка разнообразия типов задач
            task_types = set()
            for task in all_tasks:
                tag = task.tag.split('}')[-1]
                task_types.add(tag)
            if len(task_types) >= 2:
                score += self.rules['task_types']['weight']
                errors['task_types'] = True
            else:
                recommendations.append(self.rules['task_types']['message'])
                errors['task_types'] = False

            # Проверка пулов и дорожек
            lanes_valid = False
            if elements_cache['collaboration'] or elements_cache['lane_sets']:
                for lane_set in elements_cache['lane_sets'] or []:
                    lanes = lane_set.findall('.//bpmn:lane', self.namespace)
                    for lane in lanes:
                        flow_refs = lane.findall('.//bpmn:flowNodeRef', self.namespace)
                        if flow_refs:
                            lanes_valid = True
                            break
                    if lanes_valid:
                        break
            if lanes_valid:
                score += self.rules['pool_lanes']['weight']
                errors['pool_lanes'] = True
            else:
                recommendations.append(self.rules['pool_lanes']['message'])
                errors['pool_lanes'] = False

            # Проверка промежуточных событий
            event_types = set()
            for event in elements_cache['intermediate_events']:
                event_type = event.get('eventDefinitionRef', '').split(':')[-1]
                if event_type:
                    event_types.add(event_type)
            if len(event_types) >= 1:
                score += self.rules['event_types']['weight']
                errors['event_types'] = True
            else:
                recommendations.append(self.rules['event_types']['message'])
                errors['event_types'] = False

            # Проверка документации
            documented_elements = root.findall('.//bpmn:documentation', self.namespace)
            if total_elements > 0 and len(documented_elements) / total_elements >= 0.5:
                score += self.rules['documentation']['weight']
                errors['documentation'] = True
            else:
                recommendations.append(self.rules['documentation']['message'])
                errors['documentation'] = False

            # Оптимизация: правки вносятся в копию, иначе они не попадут
            # в сериализованный optimized_bpmn.
            for gateway in optimized_root.findall('.//bpmn:exclusiveGateway', self.namespace):
                outgoing = gateway.findall('bpmn:outgoing', self.namespace)
                for out in outgoing:
                    flow = optimized_root.find(f".//*[@id='{out.text}']")
                    if flow is not None and flow.find('bpmn:conditionExpression', self.namespace) is None:
                        condition = ET.SubElement(flow, f'{{{BPMN_NS}}}conditionExpression')
                        condition.text = "true"

            optimized_bpmn = ET.tostring(optimized_root, encoding='unicode')

            return {
                'score': min(100, score),
                'recommendations': recommendations,
                'details': errors,
                'optimized_bpmn': optimized_bpmn
            }
        except Exception as e:
            return {
                'score': 0,
                'recommendations': [f'Ошибка валидации BPMN: {str(e)}'],
                'details': {},
                'optimized_bpmn': bpmn_xml
            }