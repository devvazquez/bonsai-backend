"""Fixed phrases the glasses carry, in every language.

They live here and not in the firmware so that changing one is a deploy instead
of reflashing every pair of glasses. Written by hand, not machine-translated:
four short phrases the device says out loud, so nobody wants a surprise there.

The ids are a contract with the firmware (`Audio::DefaultAudios` asks for them
by name): texts and languages can change freely, renaming an id cannot.
"""

from __future__ import annotations

# The clips. This is what you edit to change what the glasses say.
CLIPS: dict[str, dict[str, str]] = {
    "no_wifi": {
        "ca": "M'he quedat sense connexió Wifi, si us plau, "
              "connecteu-me a una xarxa Wifi.",
        "es": "Me he quedado sin conexión Wifi, por favor, "
              "conéctame a una red Wifi.",
        "en": "I've lost the Wi-Fi connection. Please connect me "
              "to a Wi-Fi network.",
    },
    # Plays while the photo uploads. Must match ASK_WAKE_REPLY: that is what
    # /ask tells the model was already said.
    "start_talking": {
        "ca": "Digue'm",
        "es": "Dime",
        "en": "Tell me",
    },
    # The longest one: 11,8 s and 379 KB as 16 kHz WAV, measured.
    "first_boot": {
        "ca": "Hola, sóc el teu Bonsai. Estic preparat per escoltar-te i "
              "respondre les teves preguntes. Si us plau, connecta'm a una "
              "xarxa Wifi per poder accedir a la informació que necessito "
              "per respondre't.",
        "es": "Hola, soy tu Bonsai. Estoy listo para escucharte y responder "
              "a tus preguntas. Por favor, conéctame a una red Wifi para "
              "poder acceder a la información que necesito para responderte.",
        "en": "Hello, I'm your Bonsai. I'm ready to listen and answer your "
              "questions. Please connect me to a Wi-Fi network so I can reach "
              "the information I need to answer you.",
    },
    "missing_config": {
        "ca": "Falten dades per configurar.",
        "es": "Faltan datos por configurar.",
        "en": "Some settings are missing.",
    },
}


def ids() -> list[str]:
    return list(CLIPS)


def idiomas_de(clip_id: str) -> list[str]:
    return sorted(CLIPS.get(clip_id, {}))


def texto(clip_id: str, lang: str) -> str | None:
    """The clip in that language, or None. No fallback: a phrase in the wrong
    language is worse than a visible gap."""
    return CLIPS.get(clip_id, {}).get((lang or "").lower())


def textos_de(lang: str) -> dict[str, str]:
    lang = (lang or "").lower()
    return {
        clip_id: idiomas[lang]
        for clip_id, idiomas in CLIPS.items()
        if lang in idiomas
    }
