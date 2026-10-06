"""Friendly default names for Pis: adjective-noun, derived from the board's serial.

The serial is burned into the chip, so a board keeps its name even if it gets a new
identity (reflashed SD card). 256 x 256 = 65,536 names; listings also show a short
key tag to tell apart the rare duplicate.
"""
import hashlib
import re

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")   # valid Pi names and aliases

ADJECTIVES = """
able agile airy alert amber amiable ample ancient apt arctic ardent ashen astral autumn azure
balmy bashful beaming bold bouncy brave breezy bright brilliant brisk broad bronze bubbly busy
calm candid careful cheerful cheery chilly civil clever cloudy coastal cobalt cool copper
cosmic cozy crimson crisp curious dapper daring dazzling deep deft dewy diligent dizzy dreamy
dusky dusty eager early earnest easy ebony electric elegant emerald epic even exact fabled fair
fancy fast fearless feisty fiery firm fleet fluffy foggy fond frank free fresh frosty gentle
giant gilded glad gleaming glossy glowing golden graceful grand grassy gray great green gusty
handy happy hardy hazy hearty hidden hollow honest humble icy ideal indigo inky ivory jade
jaunty jolly jovial joyful jubilant keen kind lanky large lavish leafy level lilac limber
lively lofty lone loyal lucky lunar lush magic majestic marble mellow merry mighty mild minty
misty modest mossy murky mystic narrow neat nimble noble novel oaken ocean olive opal orange
orchid pale patient peachy pearly peppy perky placid plain playful plucky plush polar polished
prime proud quick quiet quirky radiant rainy rapid rare ready regal rich ripe rocky rosy round
royal ruby rugged rustic sage sandy scarlet serene shady sharp shiny silent silky silver simple
sleek sleepy slim smooth snowy soft solar solid sonic spare sparkly spicy spry steady stellar
stony stormy sturdy sunny super swift tall tame tawny tender thrifty tidal tidy tiny topaz
tranquil true trusty twilight upbeat urban valiant velvet verdant vivid wandering warm wavy
wild windy wise witty wooden woolly zany zealous zesty
""".split()

NOUNS = """
acorn albatross alder anchor antelope apple arbor ash aspen atlas aurora badger bamboo banjo
barley basil bay beacon bear beaver beetle bell birch bison blossom bluebird bobcat bonsai
boulder bramble breeze brook buffalo butte cactus cairn calla camel canary canyon cardinal
caribou cedar cello cheetah cherry chipmunk cicada cliff clover cobra comet compass condor
coral cougar cove coyote crane creek cricket crow cypress dahlia daisy delta dingo dipper
dolphin dove dragon drum dune eagle egret elk elm ember ermine falcon fawn fennec fern ferret
festival fiddle finch firefly fjord flamingo flute fox gannet garnet gazelle gecko geyser
glacier glade gopher grove gull harbor hare harp hawk hazel heather hedgehog heron hickory hill
horizon hornet husky ibis iguana iris island jackal jaguar jasmine jay juniper kayak kestrel
kettle kite kiwi koala lagoon lake lantern lark laurel lemur leopard lily linden lion lizard
llama lotus lynx macaw magpie mallard mango mantis maple marlin marmot marsh meadow meerkat
mesa mink mole moose moth mountain narwhal nautilus nectar newt nightjar oak oasis ocelot
octopus orca orchard oriole osprey otter owl panda panther parrot pebble pelican penguin pepper
petrel pika pine plover pond poplar prairie puffin puma quail quartz rabbit raven reed reef
ridge river robin rook rose rowan sable salmon sapling seal sequoia shark shell shore sparrow
spruce squid squirrel starling stoat stream summit swallow swan sycamore tamarin tapir teal
tern thistle thrush tiger tortoise toucan trail trout tulip tundra turtle valley vine viola
violet vole walnut walrus warbler wasp waxwing willow wolf wombat woodpecker wren yak yarrow
zebra zephyr
""".split()

assert len(ADJECTIVES) == 256 and len(set(ADJECTIVES)) == 256, len(set(ADJECTIVES))
assert len(NOUNS) == 256 and len(set(NOUNS)) == 256, len(set(NOUNS))
assert not set(ADJECTIVES) & set(NOUNS)


def name_for(hardware_id):
    """Stable adjective-noun name for a board serial (or any stable id)."""
    h = hashlib.sha256(hardware_id.encode()).digest()
    return "%s-%s" % (ADJECTIVES[h[0]], NOUNS[h[1]])
