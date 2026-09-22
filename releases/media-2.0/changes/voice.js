(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.Relax47Voice = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
  ];

  const PRESENTATION = [
    ["presentation_location", "Посёлок и расположение", "Дом находится в охраняемом посёлке «Балтийская слобода 2», примерно в двадцати минутах от КАД при обычной дорожной обстановке. У въезда работают магазины, кафе и автозаправка."],
    ["presentation_lake", "Озеро и отдых рядом", "Примерно в ста метрах находится озеро площадью около двух с половиной гектаров, с песчаным пляжем и пирсом. Рядом есть детские и спортивные площадки, прогулочные маршруты, рыбалка и конные прогулки."],
    ["presentation_house", "Дом и участок", "Современный дом расположен на благоустроенном участке шестнадцать соток. Для гостей доступны вместительная парковка, две террасы, два балкона и архитектурная вечерняя подсветка."],
    ["presentation_living", "Гостиная и второй свет", "Центр дома — просторная гостиная, объединённая с кухней и столовой. Второй свет и круговой балкон сохраняют ощущение большого открытого пространства."],
    ["presentation_bedrooms", "Спальни и ванные комнаты", "В доме четыре спальни с новой мебелью и ортопедическими матрасами, две просторные душевые, гидромассажная ванна и отдельные санузлы на этажах."],
    ["presentation_kitchen", "Оснащённая кухня", "Кухня оборудована варочной поверхностью, духовкой, микроволновой печью, холодильником с морозильным отделением, посудомоечной машиной, фильтром воды и чайником. Посуда, столовые приборы, кастрюли, сковороды и принадлежности для приготовления уже на месте."],
    ["presentation_comfort", "Бытовой комфорт", "Постельное бельё и полотенца подготовлены. Доступны стиральная машина, утюг с гладильной доской, фен и места для хранения; в комнатах есть москитные сетки и шторы блэкаут."],
    ["presentation_menu", "Главное меню умного дома", "Управление домом собрано в одном интерфейсе: главная, интерактивный план, управление, Wi-Fi, профиль гостя и помощь."],
    ["presentation_lights", "Свет на интерактивном плане", "На реальном плане выберите доступный этаж и коснитесь нужной точки. План сразу подтверждает новое состояние освещения.", "lights"],
    ["presentation_climate", "Отопление и проветривание", "Для помещений доступен индивидуальный температурный режим. Откройте климатическую точку, выберите температуру и подождите, пока система плавно её достигнет.", "climate"],
    ["presentation_territory", "Территория и барбекю", "В разделе управления доступны разрешённые вам ворота, камера и наружное освещение. Для отдыха подготовлены веранда, садовая мебель и зона барбекю с решётками и шампурами.", "gate"],
    ["presentation_vehicles", "Автомобили и пропуска", "В разделе «Автомобили и пропуска» отображаются автомобили текущего заезда и история каждого въезда и выезда. Чтобы оформить пропуск, откройте «Оформить пропуск», введите государственный номер и при необходимости комментарий, затем отправьте заявку администратору. Здесь же можно выбрать голосовой режим: только первый приезд, каждый въезд и выезд или без голосового информирования.", "gate"],
    ["presentation_multimedia", "Мультимедиа", "Телевизоры, музыкальная система, караоке и светомузыка помогают выбрать настроение отдыха.", "music"],
    ["presentation_spa", "SPA-комплекс", "Русская парная, бассейн и водопад доступны только по вашему пакету и расписанию.", "spa"],
    ["presentation_help", "Wi-Fi, профиль и помощь", "Данные Wi-Fi и QR-код находятся в одноимённом разделе. В профиле видны ваши даты, разрешённые зоны и доступы. Если понадобится помощь, откройте одноимённый раздел и свяжитесь с администратором."],
    ["presentation_alice", "Голосовой помощник Алиса", "Скажите: «Алиса, включи навык Помощник по дому». После запуска навыка Алиса переходит в режим помощника Relax47: можно свободно задавать вопросы о доме, доступных функциях, правилах и отдыхе. Чтобы завершить диалог, скажите: «Алиса, хватит»."],
  ];

  const RULES = [
    ["rule_quiet", "Тишина после 22:00", "После двадцати двух часов соблюдайте тишину на улице и не включайте громкую музыку."],
    ["rule_doors", "Двери и окна", "Ночью не оставляйте наружные двери и окна открытыми надолго."],
    ["rule_fire", "Открытый огонь", "Не разводите огонь в доме. Используйте только специально оборудованные места."],
    ["rule_fireworks", "Пиротехника запрещена", "Салюты, фейерверки, петарды и другая пиротехника запрещены на всей территории посёлка."],
    ["rule_smoking", "Курение", "Курите только в специально обозначенных местах."],
    ["rule_equipment", "Оборудование и мебель", "Не перемещайте крупную мебель и не меняйте настройки инженерных систем. При необходимости обратитесь к администратору."],
    ["rule_security", "Технический контроль для комфорта", "Пожалуйста, не закрывайте камеры и датчики. Уличный режим тишины контролируется акустическими датчиками, открытие дверей — контактными датчиками, а проход в закрытые зоны — камерами и датчиками присутствия. Системы не требуют действий от гостей и помогают бережно соблюдать ограничения ради общего комфорта и безопасности."],
    ["rule_clean", "Порядок и мусор", "Сохраняйте порядок и выбрасывайте мусор только в предусмотренных местах."],
  ];

  const SPA_RULES = [
    ["spa_rule_schedule", "Ваше время в SPA", "Пользуйтесь SPA только в назначенное вам время."],
    ["spa_rule_water", "Чистая и безопасная вода", "Не приносите к бассейну еду, посуду и алкоголь и ничего не выливайте в воду."],
    ["spa_rule_heater", "Каменка: только чистая вода", "На каменку подавайте только чистую воду, без химии, масел и ароматизаторов."],
    ["spa_rule_filter", "Фильтрацию оставим автоматике", "Не отключайте и не перенастраивайте фильтрацию и доочистку."],
    ["spa_rule_skimmer", "Скиммер и водозабор", "Не закрывайте решётки и держите руки, волосы и предметы подальше от водозабора."],
    ["spa_rule_engineering", "Оборудование обслуживает персонал", "Газовое, электрическое и инженерное оборудование обслуживает персонал."],
  ];

  function text(value, fallback = "") {
    const normalized = String(value ?? "").trim();
    return normalized && !["unknown", "unavailable", "none", "null"].includes(normalized.toLowerCase()) ? normalized : fallback;
  }

  function number(value) {
    if (value === null || value === "" || typeof value === "boolean") return null;
    const parsed = Number(String(value).replace(",", "."));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 1}).format(value);
  }

  function degreeWord(value) {
    if (!Number.isInteger(value)) return "градуса";
    const tens = Math.abs(value) % 100;
    const units = Math.abs(value) % 10;
    if (tens >= 11 && tens <= 14) return "градусов";
    if (units === 1) return "градус";
    if (units >= 2 && units <= 4) return "градуса";
    return "градусов";
  }

  function timePlus(start, durationHours) {
    const match = String(start || "").match(/^(\d{1,2}):(\d{2})$/);
    if (!match) return "";
    const total = Number(match[1]) * 60 + Number(match[2]) + Math.round((Number(durationHours) || 0) * 60);
    return `${String(Math.floor(total / 60) % 24).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  }

  function dateKey(value) {
    return String(value || "").slice(0, 10);
  }

  function dayDifference(date, now) {
    const parse = (value) => {
      const match = dateKey(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
      return match ? Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : NaN;
    };
    const left = parse(date);
    const right = parse(now);
    return Number.isFinite(left) && Number.isFinite(right) ? Math.round((left - right) / 86400000) : null;
  }

  function naturalDate(date, now) {
    const difference = dayDifference(date, now);
    if (difference === 0) return "сегодня";
    if (difference === 1) return "завтра";
    const match = dateKey(date).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return match ? `${Number(match[3])} ${MONTHS[Number(match[2]) - 1]}` : "в выбранный день";
  }

  function joinNatural(items) {
    if (items.length < 2) return items[0] || "";
    return `${items.slice(0, -1).join(", ")}, а ${items.at(-1)}`;
  }

  function spaScheduleText(context) {
    const spa = context.spa || {};
    if (!spa.enabled) return "";
    const sessions = [...(spa.sessions || [])]
      .filter((session) => session && session.start)
      .sort((a, b) => `${dateKey(a.date)} ${a.start}`.localeCompare(`${dateKey(b.date)} ${b.start}`));
    if (!sessions.length) return "Для вас также предусмотрен SPA-комплекс; время посещения можно согласовать с администратором.";
    const daily = sessions.find((session) => session.repeat === "daily");
    if (daily) {
      const startDate = dateKey(daily.date) ? `, начиная ${naturalDate(daily.date, context.now)}` : "";
      return `Для вас также предусмотрен SPA-комплекс: ежедневно с ${daily.start} до ${timePlus(daily.start, daily.durationHours)}${startDate}.`;
    }
    const spoken = sessions.slice(0, 3).map((session) => `${naturalDate(session.date, context.now)} с ${session.start} до ${timePlus(session.start, session.durationHours)}`);
    const more = sessions.length > 3 ? "; полное расписание показано на экране" : "";
    return `Для вас также предусмотрен SPA-комплекс: ${joinNatural(spoken)}${more}.`;
  }

  function formatStayDateTime(value) {
    const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
    if (!match) return "по данным вашего бронирования";
    const time = match[4] ? ` в ${match[4]}:${match[5]}` : "";
    return `${Number(match[3])} ${MONTHS[Number(match[2]) - 1]} ${match[1]} года${time}`;
  }

  function stayPeriodText(context) {
    if (context.activeStay === false) return "";
    return `с ${formatStayDateTime(context.checkInAt || context.now)} по ${formatStayDateTime(context.checkoutAt)}`;
  }

  function floorAccessText(context) {
    const floors = [...new Set((context.floors || []).map(Number).filter((floor) => floor >= 1 && floor <= 3))].sort();
    if (!floors.length) return "Доступные этажи показаны в вашем профиле.";
    const thirdFloorIncluded = floors.includes(3);
    if (thirdFloorIncluded) {
      const rooms = Math.min(4, Math.max(1, Math.round(Number(context.thirdFloorRooms) || 4)));
      if (rooms >= 4) return "В вашем распоряжении первый и второй этажи, а также третий этаж целиком.";
      const roomCount = {1:"одна", 2:"две", 3:"три"}[rooms];
      const roomWord = rooms === 1 ? "комната" : "комнаты";
      return `В вашем распоряжении первый и второй этажи, а также ${roomCount} ${roomWord} на третьем этаже.`;
    }
    if (floors.includes(1) && floors.includes(2)) return "В вашем распоряжении первый и второй этажи.";
    const names = {1: "первый этаж", 2: "второй этаж", 3: "третий этаж"};
    return `В вашем распоряжении ${joinNatural(floors.map((floor) => names[floor]))}.`;
  }

  function welcome(context) {
    if (context.activeStay === false) {
      return "Уважаемые гости! Добро пожаловать в Relax47. Сейчас коротко покажу дом, доступные возможности и всё, что находится рядом.";
    }
    const guestName = text(context.guestName, "");
    const greeting = guestName ? `${guestName}, добро пожаловать в Relax47!` : "Уважаемые гости! Добро пожаловать в Relax47.";
    const period = stayPeriodText(context);
    const spa = spaScheduleText(context);
    return `${greeting} Ваше время проживания — ${period}. ${floorAccessText(context)}${spa ? ` ${spa}` : ""} За несколько минут покажу дом, доступные вам возможности умного дома и всё, что находится рядом.`;
  }

  function temperaturePhrase(context) {
    const sauna = number(context.saunaTemperature);
    const pool = number(context.poolWaterTemperature);
    const parts = [];
    if (sauna !== null) parts.push(`температура в парной ${formatNumber(sauna)} ${degreeWord(sauna)}`);
    if (pool !== null) parts.push(`температура воды в бассейне ${formatNumber(pool)} ${degreeWord(pool)}`);
    return parts.length ? `${parts.join(", ")}. ` : "";
  }

  function payload(eventId, context, message, priority = "info") {
    return {
      eventId,
      message: message.replace(/\s+/g, " ").trim(),
      priority,
      target: text(context.target, "Планшет"),
      fallbackTarget: text(context.fallbackTarget, "Колонки дома"),
      rate: [0.8, 1, 1.2].includes(Number(context.rate)) ? Number(context.rate) : 1,
    };
  }

  function buildEvent(eventId, context = {}) {
    const name = text(context.guestName, "Дорогие гости");
    const checkout = text(context.checkoutTime, text(context.checkoutAt).slice(11, 16) || "13:00");
    const minutes = Math.max(0, Number(context.minutesUntil) || 0);
    const end = text(context.endTime, "указанного времени");
    const door = text(context.doorName, "Наружная дверь");
    const messages = {
      welcome: () => welcome(context),
      spa_soon: () => `${name}, через ${minutes} минут, в ${text(context.startTime, "указанное время")}, начнётся ваш сеанс в SPA-комплексе. Температура будет поддерживаться до ${end}.${context.spa?.rulesSeen ? "" : " Если вы ещё не знакомились с короткими правилами SPA, откройте их на экране — это займёт около минуты."}`,
      spa_ready: () => `${name}, SPA-комплекс готов. Ваш сеанс начался, ${temperaturePhrase(context)}температура будет поддерживаться до ${end}. Желаем приятного отдыха!`,
      spa_end: () => `${name}, ваш сеанс в SPA-комплексе завершился, и период поддержания температуры завершён. Благодарим вас и желаем приятного продолжения отдыха!`,
      door_open: () => context.repeat
        ? `${door} всё ещё открыта. Пожалуйста, закройте её.`
        : `${door} уже некоторое время остаётся открытой. Пожалуйста, закройте её, чтобы сохранить тепло и обеспечить корректную работу систем дома. Спасибо!`,
      checkout_first: () => `${name}, до выезда в ${checkout} осталось ${minutes} минут. Можно неспешно собираться и проверить личные вещи. Надеемся, отдых вам понравился.`,
      checkout_second: () => `${name}, до выезда в ${checkout} осталось ${minutes} минут. Пожалуйста, проверьте комнаты и личные вещи перед дорогой.`,
      farewell: () => `${name}, спасибо, что выбрали Relax47 и были нашими гостями! Надеемся, вам понравился отдых и в доме было комфортно. Желаем счастливой дороги и будем рады видеть вас снова!`,
    };
    if (!messages[eventId]) throw new Error(`Unknown voice event: ${eventId}`);
    const important = new Set(["spa_soon", "spa_ready", "spa_end", "door_open", "checkout_first", "checkout_second", "farewell"]);
    return payload(eventId, context, messages[eventId](), important.has(eventId) ? "important" : "info");
  }

  function presentationCore(context = {}) {
    const scenes = [{id: "welcome", title: "Добро пожаловать", message: welcome(context), priority: "info"}];
    for (const [id, title, message, feature] of PRESENTATION) {
      if (!feature || context.features?.[feature]) scenes.push({id, title, message, priority: "info"});
    }
    return scenes;
  }

  function buildPresentation(context = {}, standalone = true) {
    const scenes = presentationCore(context);
    if (standalone) {
      scenes.push({
        id: "presentation_finish",
        title: "Презентация завершена",
        message: "Всё необходимое находится на главном экране. Приятного отдыха в Relax47!",
        priority: "info",
      });
    }
    return scenes;
  }

  function buildRules(context = {}, standalone = true) {
    const introduction = standalone
      ? `Уважаемые гости, перед отдыхом — восемь коротких правил на период проживания ${stayPeriodText(context)}. Они помогают сохранить комфорт и безопасность дома.`
      : "А теперь — несколько важных правил.";
    const scenes = [{
      id: standalone ? "rules_welcome" : "bridge_to_rules",
      title: standalone ? "Правила проживания" : "Важные правила",
      message: introduction,
      priority: "important",
    }];
    for (const [id, title, message] of RULES) scenes.push({id, title, message, priority: "important"});
    if (context.spa?.enabled) {
      for (const [id, title, message] of SPA_RULES) {
        const schedule = id === "spa_rule_schedule" ? `${spaScheduleText(context)} ${message}` : message;
        scenes.push({id, title, message:schedule, priority:"important"});
      }
    }
    if (standalone) {
      scenes.push({
        id: "rules_finish",
        title: "Правила завершены",
        message: "Спасибо. Все правила доступны в интерфейсе в любое время. Приятного отдыха!",
        priority: "info",
      });
    }
    return scenes;
  }

  function buildTour(context = {}, mode = "full") {
    const houseIds = new Set(["welcome","presentation_location","presentation_lake","presentation_house","presentation_living","presentation_bedrooms","presentation_kitchen","presentation_comfort","presentation_territory","presentation_spa"]);
    const core = presentationCore(context);
    const house = core.filter(scene => houseIds.has(scene.id));
    const smart = core.filter(scene => !houseIds.has(scene.id));
    const rules = buildRules(context,true);
    if (mode === "presentation") return house;
    if (mode === "smart") return smart;
    if (mode === "rules") return rules;
    return [...house,...smart,...rules];
  }

  return {buildEvent, buildTour, buildPresentation, buildRules, spaScheduleText};
});

