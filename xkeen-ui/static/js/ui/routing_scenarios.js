import { stripJsonComments } from '../util/strip_json_comments.js';

export const ROUTING_SCENARIO_NORMAL = 'normal';
export const ROUTING_SCENARIO_MOBILE_WHITELIST = 'mobile-whitelist';

export const ROUTING_SCENARIO_RULE_PREFIX = 'xk_scenario_mobile_whitelist_';
export const ROUTING_SCENARIO_MOBILE_BALANCER_TAG = 'xk_mobile_whitelist';
export const ROUTING_SCENARIO_MOBILE_SELECTOR = 'white_list';

const DIRECT_INBOUNDS = Object.freeze(['redirect', 'tproxy']);

const RU_SERVICE_DOMAINS = Object.freeze([
  'ext:geosite_v2fly.dat:category-ru',
  'ext:geosite_v2fly.dat:steam',
  'domain:ozon.ru',
  'domain:wildberries.ru',
  'domain:wb.ru',
  'domain:sber.ru',
  'domain:sberbank.ru',
  'domain:tbank.ru',
  'domain:tinkoff.ru',
  'domain:vtb.ru',
  'domain:alfabank.ru',
  'domain:gazprombank.ru',
  'domain:raiffeisen.ru',
  'domain:mkb.ru',
  'domain:pochtabank.ru',
  'domain:gosuslugi.ru',
  'domain:nalog.gov.ru',
  'domain:mos.ru',
  'domain:yandex.ru',
  'domain:mail.ru',
  'domain:vk.com',
  'domain:avito.ru',
  'domain:2gis.ru',
  'regexp:.*\\.ru$',
  'regexp:.*\\.xn--p1ai$',
]);

const RU_IPCIDR = Object.freeze([
  'ext:zkeenip.dat:ru',
]);

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function isPlainObject(value) {
  return !!(value && typeof value === 'object' && !Array.isArray(value));
}

function normalizeMode(mode) {
  const text = String(mode || '').trim().toLowerCase();
  return text === ROUTING_SCENARIO_MOBILE_WHITELIST ? ROUTING_SCENARIO_MOBILE_WHITELIST : ROUTING_SCENARIO_NORMAL;
}

function managedRuleTag(rule) {
  return isPlainObject(rule) ? String(rule.ruleTag || '').trim() : '';
}

function isManagedScenarioRule(rule) {
  return managedRuleTag(rule).startsWith(ROUTING_SCENARIO_RULE_PREFIX);
}

function isManagedScenarioBalancer(item) {
  return isPlainObject(item) && String(item.tag || '').trim() === ROUTING_SCENARIO_MOBILE_BALANCER_TAG;
}

function routingTarget(root) {
  if (isPlainObject(root) && isPlainObject(root.routing)) {
    return { root, routing: root.routing };
  }
  if (
    isPlainObject(root)
    && (Array.isArray(root.rules) || Array.isArray(root.balancers) || root.domainStrategy != null)
  ) {
    return { root, routing: root };
  }
  throw new Error('routing object not found');
}

function mobileRules() {
  return [
    {
      type: 'field',
      ruleTag: `${ROUTING_SCENARIO_RULE_PREFIX}direct_private`,
      inboundTag: Array.from(DIRECT_INBOUNDS),
      outboundTag: 'direct',
      ip: [
        '127.0.0.0/8',
        '10.0.0.0/8',
        '172.16.0.0/12',
        '192.168.0.0/16',
        '169.254.0.0/16',
      ],
    },
    {
      type: 'field',
      ruleTag: `${ROUTING_SCENARIO_RULE_PREFIX}block_windows_udp`,
      inboundTag: Array.from(DIRECT_INBOUNDS),
      outboundTag: 'block',
      network: 'udp',
      port: '135,137-139',
    },
    {
      type: 'field',
      ruleTag: `${ROUTING_SCENARIO_RULE_PREFIX}block_ads`,
      inboundTag: Array.from(DIRECT_INBOUNDS),
      outboundTag: 'block',
      domain: ['ext:geosite_v2fly.dat:category-ads-all'],
    },
    {
      type: 'field',
      ruleTag: `${ROUTING_SCENARIO_RULE_PREFIX}block_quic`,
      inboundTag: Array.from(DIRECT_INBOUNDS),
      outboundTag: 'block',
      network: 'udp',
      port: '443,8443',
    },
    {
      type: 'field',
      ruleTag: `${ROUTING_SCENARIO_RULE_PREFIX}direct_bittorrent`,
      inboundTag: Array.from(DIRECT_INBOUNDS),
      outboundTag: 'direct',
      protocol: ['bittorrent'],
    },
    {
      type: 'field',
      ruleTag: `${ROUTING_SCENARIO_RULE_PREFIX}direct_ru_domains`,
      inboundTag: Array.from(DIRECT_INBOUNDS),
      outboundTag: 'direct',
      domain: Array.from(RU_SERVICE_DOMAINS),
    },
    {
      type: 'field',
      ruleTag: `${ROUTING_SCENARIO_RULE_PREFIX}direct_ru_ip`,
      inboundTag: Array.from(DIRECT_INBOUNDS),
      outboundTag: 'direct',
      ip: Array.from(RU_IPCIDR),
    },
    {
      type: 'field',
      ruleTag: `${ROUTING_SCENARIO_RULE_PREFIX}catch_all`,
      inboundTag: Array.from(DIRECT_INBOUNDS),
      balancerTag: ROUTING_SCENARIO_MOBILE_BALANCER_TAG,
      network: 'tcp,udp',
    },
  ];
}

function mobileBalancer() {
  return {
    tag: ROUTING_SCENARIO_MOBILE_BALANCER_TAG,
    selector: [ROUTING_SCENARIO_MOBILE_SELECTOR],
    strategy: { type: 'leastPing' },
    fallbackTag: 'block',
  };
}

export function parseRoutingScenarioText(text) {
  const cleaned = stripJsonComments(String(text ?? ''));
  return JSON.parse(cleaned || '{}');
}

export function detectRoutingScenarioFromObject(config) {
  try {
    const target = routingTarget(config);
    const routing = target.routing;
    const rules = Array.isArray(routing.rules) ? routing.rules : [];
    const balancers = Array.isArray(routing.balancers) ? routing.balancers : [];
    if (rules.some(isManagedScenarioRule) || balancers.some(isManagedScenarioBalancer)) {
      return ROUTING_SCENARIO_MOBILE_WHITELIST;
    }
  } catch (e) {
    return '';
  }
  return ROUTING_SCENARIO_NORMAL;
}

export function detectRoutingScenarioFromText(text) {
  try {
    return detectRoutingScenarioFromObject(parseRoutingScenarioText(text));
  } catch (e) {
    return '';
  }
}

export function applyRoutingScenarioToObject(config, mode) {
  const nextMode = normalizeMode(mode);
  const root = cloneJson(isPlainObject(config) ? config : {});
  const target = routingTarget(root);
  const routing = target.routing;
  const currentRules = Array.isArray(routing.rules) ? routing.rules : [];
  const currentBalancers = Array.isArray(routing.balancers) ? routing.balancers : [];

  const userRules = currentRules.filter((rule) => !isManagedScenarioRule(rule));
  const userBalancers = currentBalancers.filter((item) => !isManagedScenarioBalancer(item));

  if (nextMode === ROUTING_SCENARIO_MOBILE_WHITELIST) {
    if (!routing.domainStrategy) routing.domainStrategy = 'IPIfNonMatch';
    routing.rules = mobileRules().concat(userRules);
    routing.balancers = [mobileBalancer()].concat(userBalancers);
  } else {
    routing.rules = userRules;
    routing.balancers = userBalancers;
  }

  return root;
}

export function applyRoutingScenarioText(text, mode) {
  const before = String(text ?? '');
  const parsed = parseRoutingScenarioText(before);
  const next = applyRoutingScenarioToObject(parsed, mode);
  const nextText = JSON.stringify(next, null, 2) + '\n';
  return {
    mode: normalizeMode(mode),
    text: nextText,
    changed: nextText !== before,
  };
}

export const routingScenarios = Object.freeze({
  normal: ROUTING_SCENARIO_NORMAL,
  mobileWhitelist: ROUTING_SCENARIO_MOBILE_WHITELIST,
  managedRulePrefix: ROUTING_SCENARIO_RULE_PREFIX,
  mobileBalancerTag: ROUTING_SCENARIO_MOBILE_BALANCER_TAG,
  mobileSelector: ROUTING_SCENARIO_MOBILE_SELECTOR,
  applyText: applyRoutingScenarioText,
  detectText: detectRoutingScenarioFromText,
});
