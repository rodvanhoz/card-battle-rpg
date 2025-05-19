#!/usr/bin/env python3
"""
Card Battle RPG – versão 0.3
===========================

*Ajustes solicitados*
1. Exibe **Nível** e **Poder de Luta (PL)** junto ao HP / Ki de ambos os
   combatentes.
2. Personagem ganha EXP após cada batalha; ao atingir o limiar, sobe de
   level e seus atributos (HP, Ki, PL) aumentam automaticamente.
3. Progresso do herói é salvo em **save.json** após cada batalha e é
   carregado no início, se existir.
4. Oponente gera um PL aleatório dentro de 60 %–90 % do PL médio do nível
   escolhido pelo jogador.
5. Inimigos têm nomes aleatórios.
6. Itens podem ser dropados ao final da batalha (cura ou aumento de PL).
"""

from __future__ import annotations
import json
import random
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import List, Optional, Callable

SAVE_FILE = Path("save.json")
ABILITY_CATALOG: dict[str, "Ability"] = {}
HERO_CFG: dict = {}

# ------------------------------
# 1.  Cartas
# ------------------------------

class Symbol(Enum):
    STAR = "★"      # ataque
    CIRCLE = "●"    # equilibrado
    TRIANGLE = "▲"  # defesa

    @staticmethod
    def random() -> "Symbol":
        return random.choice(list(Symbol))

@dataclass
class Card:
    number: int
    symbol: Symbol

    @property
    def atk_mul(self) -> float:
        return {Symbol.STAR: 1.4, Symbol.CIRCLE: 1.0, Symbol.TRIANGLE: 0.6}[self.symbol]

    @property
    def def_mul(self) -> float:
        return {Symbol.STAR: 0.6, Symbol.CIRCLE: 1.0, Symbol.TRIANGLE: 1.4}[self.symbol]

    def __str__(self):
        return f"[{self.number}{self.symbol.value}]"

class Deck:
    def __init__(self):
        self.cards: List[Card] = []
        self._build()

    def _build(self):
        self.cards.clear()
        for _ in range(3):
            for n in range(1, 8):
                for sym in Symbol:
                    self.cards.append(Card(n, sym))
        random.shuffle(self.cards)

    def draw(self, n: int = 5) -> List[Card]:
        if len(self.cards) < n:
            self._build()
        hand, self.cards = self.cards[:n], self.cards[n:]
        return hand

# ------------------------------
# 2.  Habilidades
# ------------------------------

AbilityFn = Callable[["Character", "Character"], None]

@dataclass
class Ability:
    name: str
    ki_cost: int
    desc: str
    effect: AbilityFn


def ki_wave(attacker: "Character", defender: "Character"):
    # usa a mesma fórmula base, mas triplica o dano normal
    atk_mul = 3.0                    # magia forte, sem carta
    def_mul = 1.0                    # defesa neutra
    dmg_base = 25 + (atk_mul - def_mul) * 8.72
    ratio    = attacker.power_level / max(1, defender.power_level)
    dmg      = int(dmg_base * ratio)
    defender.take_damage(dmg)
    type_out(f"⚡ {attacker.name} lança Onda de Ki causando {dmg} de dano!")


def heal(attacker: "Character", _):
    amount = int(attacker.max_hp * 0.3)
    attacker.hp = min(attacker.max_hp, attacker.hp + amount)
    type_out(f"✨ {attacker.name} recupera {amount} HP!")

def kaioken(attacker: "Character", _):
    if attacker.kaioken_on:
        print("(Kaioken já ativo)")
        return

    # ---- multiplicador ----
    if attacker.is_player:
        try:
            mult = int(input("Multiplo do Kaioken: "))
            if mult <= 0:
                raise ValueError
        except ValueError:
            print("Inválido – Kaioken cancelado.")
            return
    else:                      # veio do JSON
        mult = attacker.kaioken_mult_cfg

    cost_unit = attacker.kaioken_cost_unit   # (7 p/ herói, ou o que veio do JSON)
    cost = cost_unit * mult
    if attacker.ki < cost:
        print("(x) Ki insuficiente!")
        return

    attacker.ki          -= cost
    attacker.kaioken_on   = True
    attacker.kaioken_mult = mult
    attacker.kaioken_pl0  = attacker.power_level
    attacker.power_level  = int(attacker.power_level * (1 + 0.5 * mult))

    type_out(f"🔥 {attacker.name} ativa Kaioken ×{mult}! PL agora {attacker.power_level}.")

# ------------------------------
# 3.  Personagens
# ------------------------------

def maybe_learn_skills(char: "Character") -> None:
    learned = {ab.name for ab in char.abilities}
    for skill, req_lvl in HERO_CFG["learn"].items():
        if char.level >= req_lvl and skill not in learned:
            char.abilities.append(ABILITY_CATALOG[skill])
            type_out(f"✨ {char.name} aprendeu {skill}!")

@dataclass
class Character:
    name: str
    level: int = 1
    power_level: int = 1000
    max_hp: int = 500
    max_ki: int = 200
    abilities: List[Ability] = field(default_factory=list)
    backpack: List[str] = field(default_factory=list)

    # --- status de Kaioken (não vai para o save) ---
    kaioken_on:    bool  = field(default=False, init=False)
    kaioken_mult:  int   = field(default=0,    init=False)
    kaioken_pl0:   int   = field(default=0,    init=False)   # PL antes do boost
    kaioken_cost_unit: int = field(default=7, init=False)
    kaioken_mult_cfg: int = 0

    # transformação
    form_name: str = field(default="Base", init=False)
    form_stack: list[dict] = field(default_factory=list, init=False)

    hp: int = field(init=False)
    ki: int = field(init=False)
    exp: int = 0

    def __post_init__(self):
        self.hp = self.max_hp
        self.ki = self.max_ki

    # ----- Atributos derivados -----
    @property
    def attack(self):
        return int(self.power_level * 0.05)

    @property
    def defense(self):
        return int(self.power_level * 0.04)

# ----- Ações -----
    def play_card(self, card: Card, target: "Character", def_card: Card):
        # ---------- multiplicadores das cartas ----------
        atk_mul = card.atk_mul * (1 + card.number / 10)
        def_mul = def_card.def_mul * (1 + def_card.number / 10)

        # ---------- dano base p/ PL iguais ----------
        diff      = atk_mul - def_mul               # -1.72 … +1.72
        dmg_base  = 25 + diff * 8.72                # faixa 10-40

        # ---------- escala pelo fator de PL ----------
        ratio = self.power_level / max(1, target.power_level)
        dmg   = int(dmg_base * ratio)               # pode ser 0

        # ---------- aplica dano ----------
        target.take_damage(dmg)

        # ---------- valores para exibir ----------
        atk_msg = int(self.attack  * atk_mul)
        def_msg = int(target.defense * def_mul)

        if dmg == 0:
            type_out(f"{self.name} usa {card} mas {target.name} é muito forte!")
        else:
            type_out(f"{self.name} usa {card} causando {dmg} de dano!")
              


    def take_damage(self, dmg: int):
        self.hp = max(0, self.hp - dmg)

    def spend_ki(self, cost: int) -> bool:
        if self.ki < cost:
            print(f"(x) {self.name} não tem Ki suficiente!")
            return False
        self.ki -= cost
        return True

    def is_alive(self):
        return self.hp > 0

    # ----- Progressão -----
    def gain_exp(self, amount: int):
        self.exp += amount
        needed = 100 * self.level
        while self.exp >= needed:
            self.exp -= needed
            self.level_up()
            needed = 100 * self.level

    def level_up(self):
        self.level += 1
        self.power_level = int(self.power_level * random.uniform(1.15, 1.20))
        self.max_hp = int(self.max_hp * 1.05)
        self.max_ki = int(self.max_ki * 1.05)
        self.hp, self.ki = self.max_hp, self.max_ki
        print(f"⬆️  {self.name} subiu para nível {self.level}! PL agora {self.power_level}.")
        maybe_learn_skills(self)

    # ----- Serialização -----
    def to_dict(self):
        keep = ("name","level","power_level","max_hp","max_ki",
                "abilities","backpack","exp")
        d = {k:getattr(self,k) for k in keep}
        d["abilities"] = [ab.name for ab in self.abilities]
        d["backpack"] = self.backpack
        return d

    @staticmethod
    def from_dict(data: dict, ability_catalog: dict[str, Ability]):
        abilities = [ability_catalog[name] for name in data.pop("abilities", [])]
        char = Character(**data, abilities=abilities)
        char.backpack = data.get("backpack", [])
        return char

# ------------------------------
# 4.  Jogadores
# ------------------------------

class Player:
    def __init__(self, character: Character, is_human: bool):
        self.char           = character
        self.char.is_player = is_human      # marca p/ Kaioken ou IA

    def choose(self, hand: List[Card], enemy: Character) -> str:
        raise NotImplementedError

class HumanPlayer(Player):
    def __init__(self, character): super().__init__(character, True)
    def choose(self, hand, _):
        print("\nSuas cartas:")
        for i,c in enumerate(hand): print(f" {i+1}. {c}")
        
        extra = ""
        if can_transform(self.char, HERO_CFG) or self.char.form_stack:
            extra = "  |  t. Transformar"
        print(f" i. Item  |  a. Habilidade  |  q. Desistir{extra}")

        return input("Escolha: ").strip()

class AIPlayer(Player):
    def __init__(self, character): super().__init__(character, False)
    def choose(self, hand, _):
        # ativa Kaioken 40 % se disponível e viável
        if any(ab.name=="Kaioken" for ab in self.char.abilities) \
           and not self.char.kaioken_on \
           and self.char.ki >= self.char.kaioken_cost_unit*self.char.kaioken_mult_cfg \
           and random.random()<0.4:
            return "a"
        # ou escolhe a melhor carta
        best = max(range(len(hand)), key=lambda i: hand[i].atk_mul*hand[i].number)
        return str(best+1)

# ------------------------------
# 5.  Loop de batalha
# ------------------------------

def battle(player: Player, enemy: Player):
    deck = Deck()

    # compra a mão inicial uma única vez
    p_hand, e_hand = deck.draw(), deck.draw()
    rnd = 1

    while player.char.is_alive() and enemy.char.is_alive():
        process_kaioken(player.char)
        process_kaioken(enemy.char)
        check_revert(player.char)
        check_revert(enemy.char)
        time.sleep(1.5)
        print("\n" + "═" * 32 + f"\n🥊  ROUND {rnd}")
        rnd += 1
        print()
        show_status(player.char, enemy.char)
        time.sleep(0.4)

        # ---------- ENTRADA DO JOGADOR (validação robusta) ----------
        while True:
            print()
            p_choice = player.choose(p_hand, enemy.char).strip().lower()
            if p_choice in {"q", "i", "a", "t"}:
                break
            if p_choice.isdigit() and 1 <= int(p_choice) <= len(p_hand):
                break
            # nada escolhido ou fora do intervalo: pede de novo
            print("Escolha inválida — tente de novo.")

        if p_choice == "q":
            type_out("Você desistiu!")
            return False

        e_choice = enemy.choose(e_hand, player.char)

        p_card = choice_to_card(p_choice, p_hand)
        e_card = choice_to_card(e_choice, e_hand)

        # ---------- Jogador age ----------
        if p_choice == "i":
            use_item_menu(player.char)

        elif p_choice == "a":
            ability = pick_ability(player.char)
            if ability is None:
                rnd -= 1
                continue    # volta ao início do loop sem perder o turno
            if player.char.spend_ki(ability.ki_cost):
                ability.effect(player.char, enemy.char)

        elif p_choice == "t":
            opts = can_transform(player.char, HERO_CFG)

            # monta menu: 0 = reverter  |  1… = novas formas possíveis
            if not opts and not player.char.form_stack:
                print("(nenhuma forma disponível)")
            else:
                print(" 0. Reverter forma" if player.char.form_stack else "", end="")
                for i, t in enumerate(opts, 1):
                    print(f"\n {i}. {t['name']}", end="")
                print()
                try:
                    sel = input("Escolha: ").strip()
                    if sel == "0":
                        revert_form(player.char)
                    else:
                        idx = int(sel) - 1
                        apply_transformation(player.char, opts[idx])
                except Exception:
                    print("Inválido – transformação cancelada.")

        elif p_card:
            player.char.play_card(p_card, enemy.char, e_card or p_card)

        if not enemy.char.is_alive():
            break

        time.sleep(0.8)

        # ---------- IA age ----------
        if e_choice == "a":
            usable = [ab for ab in enemy.char.abilities if enemy.char.ki >= ab.ki_cost]
            if usable:
                ab = random.choice(usable)
                enemy.char.spend_ki(ab.ki_cost)
                ab.effect(enemy.char, player.char)
            else:
                alt = random.choice(e_hand)
                enemy.char.play_card(alt, player.char, p_card or alt)
        elif e_choice == "i":
            use_item_menu(enemy.char)
        elif e_card:
            enemy.char.play_card(e_card, player.char, p_card or e_card)

        print()

        # ---------- gerenciamento das mãos ----------
        if p_card in p_hand:
            p_hand.remove(p_card)
        if e_card in e_hand:
            e_hand.remove(e_card)

        # apenas UMA carta nova é comprada para repor cada mão
        p_hand.extend(deck.draw(5 - len(p_hand)))
        e_hand.extend(deck.draw(5 - len(e_hand)))

    victor = player if player.char.is_alive() else enemy
    print("\n" + "💥" * 10)
    type_out(f"{victor.char.name} venceu a batalha!")

    if victor is player:
        base    = random.randint(40, 60)
        factor  = enemy.char.level / max(1, player.char.level)
        gained  = int(base * factor)
        player.char.gain_exp(gained)
        print(f"🚀 Você ganhou {gained} EXP!")
        drop_item(player.char)
        
    # voltar o PL para valor base
    for ch in (player.char, enemy.char):
        # volta directamente à forma Base
        while ch.form_stack:
            last = ch.form_stack.pop()
            ch.form_name   = last["name"]
            ch.power_level = last["power_level"]
            ch.max_hp      = last["max_hp"]
            ch.max_ki      = last["max_ki"]
            ch.hp = min(ch.hp, ch.max_hp)
            ch.ki = min(ch.ki, ch.max_ki)
        ch.terminated_at = None

        if ch.kaioken_on:
            ch.power_level = ch.kaioken_pl0
            ch.kaioken_on  = False

    return victor is player

def auto_battle(c1: Character, c2: Character):
    """Luta sem intervenção do utilizador – IA vs IA (2 s entre ações)."""
    ai1, ai2 = AIPlayer(c1), AIPlayer(c2)
    deck     = Deck()
    p_hand, e_hand = deck.draw(), deck.draw()
    rnd = 1
    while c1.is_alive() and c2.is_alive():
        process_kaioken(c1)
        process_kaioken(c2)
        check_revert(c1)
        check_revert(c2)
        time.sleep(1.5)
        print("\n" + "═"*32 + f"\n🤖  ROUND {rnd}")
        rnd += 1
        show_status(c1, c2)
        time.sleep(0.5)

        print()

        c1_choice = ai1.choose(p_hand, c2)
        c2_choice = ai2.choose(e_hand, c1)
        p_card    = choice_to_card(c1_choice, p_hand)
        e_card    = choice_to_card(c2_choice, e_hand)

        # Ação do 1º monstro
        if c1_choice == "a":
            abil = random.choice([ab for ab in c1.abilities if c1.ki >= ab.ki_cost] or [None])
            if abil and c1.spend_ki(abil.ki_cost):
                abil.effect(c1, c2)
        elif p_card:

            c1.play_card(p_card, c2, e_card or p_card)

        if not c2.is_alive():
            break

        time.sleep(2)                # << intervalo pedido

        # Ação do 2º monstro
        if c2_choice == "a":
            abil = random.choice([ab for ab in c2.abilities if c2.ki >= ab.ki_cost] or [None])
            if abil and c2.spend_ki(abil.ki_cost):
                abil.effect(c2, c1)
        elif e_card:

            c2.play_card(e_card, c1, p_card or e_card)

        # repõe apenas 1 carta
        if p_card in p_hand: p_hand.remove(p_card)
        if e_card in e_hand: e_hand.remove(e_card)
        p_hand.extend(deck.draw(5 - len(p_hand)))
        e_hand.extend(deck.draw(5 - len(e_hand)))

    vencedor = c1 if c1.is_alive() else c2
    print("\n" + "💥"*10)
    type_out(f"{vencedor.name} venceu o amistoso!")


def choice_to_card(choice: str, hand: List[Card]) -> Optional[Card]:
    if choice.isdigit() and 1 <= int(choice) <= len(hand):
        return hand[int(choice) - 1]
    return None          # item, habilidade, etc.

# ------------------------------
# 6.  Utilidades
# ------------------------------

def bar(cur, mx, length=20):
    filled = int(cur / mx * length)
    return "█" * filled + "░" * (length - filled)


def show_status(c1: Character, c2: Character):
    def fmt(ch: Character) -> str:
        lvl = f"Lv {ch.level:>4}({ch.form_name[0]})"  # 4 dígitos
        pl  = f"PL {ch.power_level:>9}"               # 9 dígitos
        hp  = f"{ch.hp}/{ch.max_hp}".rjust(9)         # até 9 999 999/9 999 999
        ki  = f"{ch.ki}/{ch.max_ki}".rjust(9)
        return (f"{ch.name:12} {lvl} {pl}  HP {bar(ch.hp, ch.max_hp)} {hp}  Ki {ki}")

    print(fmt(c1))
    print(fmt(c2))


def pick_ability(char: Character) -> Optional[Ability]:
    if not char.abilities:
        print("(sem habilidades)")
        return None

    print(" 0. Voltar")
    for i, ab in enumerate(char.abilities, 1):
        print(f" {i}. {ab.name} (Ki {ab.ki_cost}) – {ab.desc}")

    try:
        choice = int(input("Habilidade: "))
        if choice == 0:
            return None
        return char.abilities[choice-1]
    except (ValueError, IndexError):
        print("Inválido")
        return None

def type_out(text: str, char_delay: float = 0.02):
    # garante que sempre começamos na coluna 0 de uma linha nova
    for ch in text:
        print(ch, end='', flush=True)
        time.sleep(char_delay)
    print()


# ----- Itens & mochila -----

def apply_item(char: Character, item: str):
    if item == "Elixir de Poder":
        char.power_level = int(char.power_level * 1.10)
        print("💪 Elixir de Poder usado! PL +10%")
    elif item == "Poção de Cura":
        heal_amt = int(char.max_hp * 0.5)
        char.hp = min(char.max_hp, char.hp + heal_amt)
        print(f"❤️ Poção de Cura usada! +{heal_amt} HP")
    else:
        print("(item desconhecido)")

def use_item_menu(char: Character):
    if not char.backpack:
        print("(mochila vazia)")
        return
    print("\nInventário:")
    for i, it in enumerate(char.backpack):
        print(f" {i+1}. {it}")
    try:
        idx = int(input("Usar item nº: ")) - 1
        item = char.backpack.pop(idx)
        apply_item(char, item)
    except (ValueError, IndexError):
        print("Inválido — item não usado.")

CREATURES_FILE = Path("creatures.json")
WORLDS_FILE    = Path("worlds.json")
HEROES_FILE    = Path("heroes_types.json")


def load_hero_config() -> dict:
    with HEROES_FILE.open() as f:
        return json.load(f)["types"]["Human"]   # só 1 tipo por enquanto

def load_creatures(ability_catalog):
    with CREATURES_FILE.open() as f:
        data = json.load(f)["monsters"]

    table = {}
    for row in data:
        abis = []
        for abil_str in row["abilities"]:
            if ":" in abil_str:                        # “Kaioken:2:7”
                name, mult, *rest = abil_str.split(":")
                abis.append(ability_catalog[name.capitalize()])
                if name.lower() == "kaioken":
                    row["_kaioken_cfg"] = (int(mult), int(rest[0]) if rest else 7)
            else:
                abis.append(ability_catalog[abil_str])
    
        char = Character(
            name=row["name"], level=row["level"], power_level=row["power_level"],
            max_hp=row["max_hp"], max_ki=row["max_ki"], abilities=abis
        )
        if "_kaioken_cfg" in row:
            mult, custo = row["_kaioken_cfg"]
            char.kaioken_mult_cfg  = mult
            char.kaioken_cost_unit = custo
        table[row["id"]] = char
    return table

def load_worlds() -> list[dict]:
    with WORLDS_FILE.open() as f:
        return json.load(f)["worlds"]

# ----- Itens -----

def drop_item(hero: Character):
    r = random.random()
    if r < 0.05:
        item = "Elixir de Poder"
    elif r < 0.25:
        item = "Poção de Cura"
    else:
        return            # 75 % de chance de não dropar nada
    hero.backpack.append(item)
    print(f"🎁 Item obtido: {item} (guardado na mochila)")


# ----- Nomes rivais -----

RIVAL_NAMES = [
    "Zorin", "Kavex", "Talor", "Vexa", "Solan", "Drax", "Myra", "Zara", "Korun", "Lunex",
    "Arkor", "Nyx", "Jorad", "Sylfa", "Cryst", "Vilor"
]

def random_rival_name() -> str:
    return random.choice(RIVAL_NAMES)

def cancel_kaioken(char: Character):
    """Desfaz Kaioken (se estiver ativo)."""
    if char.kaioken_on:
        char.power_level = char.kaioken_pl0
        char.kaioken_on  = False
        char.kaioken_mult = 0
        char.kaioken_pl0  = 0

def process_kaioken(char: "Character"):
    """Desconta Ki por turno; desliga se faltar energia."""
    if not char.kaioken_on:
        return
    if char.kaioken_mult <= 0:
        char.kaioken_on = False
        return
    cost = char.kaioken_cost_unit * char.kaioken_mult
    if char.ki >= cost:
        char.ki -= cost
    else:
        char.power_level = char.kaioken_pl0
        char.kaioken_on  = False
        type_out(f"{char.name} não tem Ki para manter o Kaioken e volta ao PL normal!")

def can_transform(char: Character, cfg: dict) -> list[dict]:
    """Lista apenas as formas que REALMENTE podem ser escolhidas agora."""
    avail = []
    for t in cfg["transformations"]:
        # ------------- dados -------------
        req = t.get("required") or t.get("requitred")  # aceita ambos
        # ------------- checagens ----------
        if char.level < t["level_enabled"]:
            continue                # nível insuficiente
        if t["name"] == char.form_name:
            continue                # já estamos nela

        if req is None:             # 1ª forma da cadeia
            if char.form_name == "Base" and not char.form_stack:
                avail.append(t)
        else:                       # forma posterior
            if char.form_name == req:
                avail.append(t)
    return avail

def apply_transformation(char: Character, trans: dict):
    # se Kaioken estiver ativo, cancela primeiro
    cancel_kaioken(char)

    # guarda estado actual
    char.form_stack.append({
        "name":        char.form_name,
        "power_level": char.power_level,
        "max_hp":      char.max_hp,
        "max_ki":      char.max_ki,
        "hp":          char.hp,
        "ki":          char.ki,
    })

    mul = trans["multipliers"]

    # máximos
    char.power_level = int(char.power_level * mul["power_level"])
    char.max_hp      = int(char.max_hp      * mul["hp"])
    char.max_ki      = int(char.max_ki      * mul["ki"])

    # valores actuais (sem cura total)
    char.hp = min(int(char.hp * mul["hp"]), char.max_hp)
    char.ki = min(int(char.ki * mul["ki"]), char.max_ki)

    char.form_name     = trans["name"]
    char.terminated_at = trans["terminated_at"]

    type_out(f"✨ {char.name} transformou-se em {char.form_name}!")

def revert_form(char: Character):
    """Volta à forma anterior e cancela Kaioken, se houver."""
    if not char.form_stack:
        print("(já está na forma Base)")
        return

    cancel_kaioken(char)                   # 1️⃣  cancela antes
    last = char.form_stack.pop()           # 2️⃣  restaura forma
    char.form_name   = last["name"]
    char.power_level = last["power_level"]
    char.max_hp      = last["max_hp"]
    char.max_ki      = last["max_ki"]

    # ajusta HP/Ki correntes ao novo máximo
    char.hp = min(char.hp, char.max_hp)
    char.ki = min(char.ki, char.max_ki)

    char.terminated_at = None
    type_out(f"↩️  {char.name} voltou à forma {char.form_name}!")

def check_revert(char: Character):
    if getattr(char, "terminated_at", None) is None:
        return
    if char.hp / char.max_hp <= char.terminated_at:
        # garante que não restará Kaioken activo
        cancel_kaioken(char)

        last = char.form_stack.pop()
        char.form_name   = last["name"]
        char.power_level = last["power_level"]
        char.max_hp      = last["max_hp"]
        char.max_ki      = last["max_ki"]
        char.hp = min(char.hp, char.max_hp)
        char.ki = min(char.ki, char.max_ki)
        char.terminated_at = None
        type_out(f"{char.name} perdeu energia e voltou à forma {char.form_name}!")

# ------------------------------
# 7.  Salvamento
# ------------------------------

def save_game(hero: Character):
    with SAVE_FILE.open("w") as f:
        json.dump(hero.to_dict(), f, indent=2)
    print("💾 Progresso salvo!")


def load_game(ability_catalog: dict[str, Ability]) -> Optional[Character]:
    if not SAVE_FILE.exists():
        return None
    try:
        with SAVE_FILE.open() as f:
            data = json.load(f)
        print("🔄 Save encontrado – carregando...")
        return Character.from_dict(data, ability_catalog)
    except Exception as e:
        print("Falha ao carregar save:", e)
        return None

# ------------------------------
# 8.  Entrada principal
# ------------------------------

def power_level_for_level(lvl: int) -> int:
    pl = 1000
    for _ in range(lvl - 1):
        pl = int(pl * 1.17)
    return pl

def hp_ki_for_level(lvl: int) -> tuple[int, int]:
    base_hp, base_ki = 500, 200
    factor = 1.1 ** (lvl - 1)
    return int(base_hp * factor), int(base_ki * factor)

import copy
import math    # já há import random/time etc.

# ---------- AMISTOSO ----------
def amistoso_menu(creatures: dict[int, Character]):
    """Mostra a lista em 3 colunas, recebe dois ids e dispara a luta automática."""
    ids = sorted(creatures)
    col = math.ceil(len(ids) / 3)
    print("\n=== MONSTROS DISPONÍVEIS ===")
    for row in range(col):
        line = ""
        for c in range(3):
            idx = row + c*col
            if idx < len(ids):
                cid  = ids[idx]
                mon  = creatures[cid]
                line += f"{cid:02}: {mon.name:<12} - PL:{mon.power_level:<7}  |  "
        print(line.rstrip(" | "))
    try:
        m1 = int(input("\nId do primeiro monstro: "))
        m2 = int(input("Id do segundo  monstro: "))
        if m1 not in creatures or m2 not in creatures or m1 == m2:
            raise ValueError
    except ValueError:
        print("Escolha inválida – voltando ao menu.")
        return

    # cópias frescas (para não alterar o protótipo)
    a = copy.deepcopy(creatures[m1])
    b = copy.deepcopy(creatures[m2])
    auto_battle(a, b)

def main():
    global ABILITY_CATALOG, HERO_CFG

    ABILITY_CATALOG = {
        "Onda de Ki": Ability("Onda de Ki", 40, "Explosão de energia", ki_wave),
        "Cura":       Ability("Cura", 30, "Recupera HP", heal),
        "Kaioken":    Ability("Kaioken", 0, "Aumenta PL (custa Ki por turno)", kaioken),
    }

    HERO_CFG = load_hero_config()

    creatures = load_creatures(ABILITY_CATALOG)
    worlds    = load_worlds()

    hero = load_game(ABILITY_CATALOG)
    if not hero:
        print("=== NOVA AVENTURA ===")
        name = input("Nome do herói: ") or "Herói"
        hero = Character(name=name, abilities=[])
        maybe_learn_skills(hero)
    else:
        maybe_learn_skills(hero)


    while True:
        print("\n=== SELECIONE UM MUNDO ===")
        for i, w in enumerate(worlds, 1):
            print(f" {i}. {w['name']}")
        print(" a. Amistoso")
        print(" q. Sair")

        sel = input("Escolha: ").strip().lower()
        if sel == "q":
            break
        if sel == "a":
            amistoso_menu(creatures)
            continue
        if not sel.isdigit() or not (1 <= int(sel) <= len(worlds)):
            print("Inválido.")
            continue

        world = worlds[int(sel) - 1]
        type_out(f"\n🌍 Entrando em {world['name']}...\n")
        for entry in world["creatures"]:
            foe_proto = creatures[entry["creature_id"]]
            foe_proto = creatures[entry["creature_id"]]
            foe       = copy.deepcopy(foe_proto)

            if not battle(HumanPlayer(hero), AIPlayer(foe)):
                type_out("⚠️  Mundo interrompido – herói derrotado.")
                save_game(hero)
                return
            save_game(hero)

        type_out(f"\n🏆 Parabéns! Você completou o mundo {world['name']}!\n")

if __name__ == "__main__":
    main()
