import numpy
from PIL import Image, ImageDraw
from itertools import izip, count
import os.path
import hashlib

import nbt
import textures
from textures import texturemap as txtarray

# General note about pasting transparent image objects onto an image with an
# alpha channel:
# If you use the image as its own mask, it will work fine only if the alpha
# channel is binary. If there's any translucent parts, then the alpha channel
# of the dest image will have its alpha channel modified. To prevent this:
# first use im.split() and take the third item which is the alpha channel and
# use that as the mask. Then take the image and use im.convert("RGB") to strip
# the image from its alpha channel, and use that as the source to paste()

def get_lvldata(filename):
    """Takes a filename and returns the Level struct, which contains all the
    level info"""
    return nbt.load(filename)[1]['Level']

def get_blockarray(level):
    """Takes the level struct as returned from get_lvldata, and returns the
    Block array, which just contains all the block ids"""
    return numpy.frombuffer(level['Blocks'], dtype=numpy.uint8).reshape((16,16,128))

def get_blockarray_fromfile(filename):
    """Same as get_blockarray except takes a filename and uses get_lvldata to
    open it. This is a shortcut"""
    level = get_lvldata(filename)
    return get_blockarray(level)

def get_skylight_array(level):
    """Returns the skylight array. Remember this is 4 bits per block, so divide
    the z component by 2 when accessing the array. and mask off the top or
    bottom 4 bits if it's odd or even respectively
    """
    return numpy.frombuffer(level['SkyLight'], dtype=numpy.uint8).reshape((16,16,64))

# This set holds blocks ids that can be seen through, for occlusion calculations
transparent_blocks = set([0, 8, 9, 18, 20, 37, 38, 39, 40, 50, 51, 52, 53, 59, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 74, 75, 76, 77, 79, 83, 85])

def render_and_save(chunkfile, cave=False):
    a = ChunkRenderer(chunkfile)
    try:
        return a.render_and_save(cave)
    except Exception, e:
        import traceback
        traceback.print_exc()
        raise

class ChunkRenderer(object):
    def __init__(self, chunkfile):
        if not os.path.exists(chunkfile):
            raise ValueError("Could not find chunkfile")
        self.chunkfile = chunkfile

    def _load_level(self):
        """Loads and returns the level structure"""
        if not hasattr(self, "_level"):
            self._level = get_lvldata(self.chunkfile)
        return self._level
    level = property(_load_level)
        
    def _load_blocks(self):
        """Loads and returns the block array"""
        if not hasattr(self, "_blocks"):
            self._blocks = get_blockarray(self._load_level())
        return self._blocks
    blocks = property(_load_blocks)

    def _hash_blockarray(self):
        """Finds a hash of the block array"""
        h = hashlib.md5()
        h.update(self.level['Blocks'])

        # If the render algorithm changes, change this line to re-generate all
        # the chunks automatically:
        h.update("1")

        digest = h.hexdigest()
        # 6 digits ought to be plenty
        return digest[:6]


    def render_and_save(self, cave=False):
        """Render the chunk using chunk_render, and then save it to a file in
        the same directory as the source image. If the file already exists and
        is up to date, this method doesn't render anything.
        """
        destdir, filename = os.path.split(self.chunkfile)
        destdir = os.path.abspath(destdir)
        blockid = ".".join(filename.split(".")[1:3])
        dest_filename = "img.{0}.{1}.{2}.png".format(
                blockid,
                "cave" if cave else "nocave",
                self._hash_blockarray(),
                )

        dest_path = os.path.join(destdir, dest_filename)

        if os.path.exists(dest_path):
            # Try to open it to see if it's corrupt or something (can happen if
            # the program crashed last time)
            try:
                testimg = Image.open(dest_path)
                testimg.load()
            except Exception:
                # guess not, continue below
                pass
            else:
                return dest_path
        else:
            # Remove old images for this chunk
            for oldimg in os.listdir(destdir):
                if oldimg.startswith("img.{0}.{1}.".format(blockid,
                        "cave" if cave else "nocave")) and \
                        oldimg.endswith(".png"):
                    os.unlink(os.path.join(destdir,oldimg))
                    break

        # Render the chunk
        img = self.chunk_render(cave=cave)
        # Save it
        img.save(dest_path)
        # Return its location
        return dest_path

    def chunk_render(self, img=None, xoff=0, yoff=0, cave=False):
        """Renders a chunk with the given parameters, and returns the image.
        If img is given, the chunk is rendered to that image object. Otherwise,
        a new one is created. xoff and yoff are offsets in the image.
        
        For cave mode, all blocks that have any direct sunlight are not
        rendered, and blocks are drawn with a color tint depending on their
        depth."""
        blocks = self.blocks
        if cave:
            skylight = get_skylight_array(self.level)
            # Cave mode. Actually go through and 0 out all blocks that are not in a
            # cave, so that it only renders caves.

            # 1st task: this array is 2 blocks per byte, expand it so we can just
            # do a bitwise and on the arrays
            skylight_expanded = numpy.empty((16,16,128), dtype=numpy.uint8)
            # Even elements get the lower 4 bits
            skylight_expanded[:,:,::2] = skylight & 0x0F
            # Odd elements get the upper 4 bits
            skylight_expanded[:,:,1::2] = skylight >> 4

            # Places where the skylight is not 0 (there's some amount of skylight
            # touching it) change it to something that won't get rendered, AND
            # won't get counted as "transparent".
            blocks = blocks.copy()
            blocks[skylight_expanded != 0] = 21


        # Each block is 24x24
        # The next block on the X axis adds 12px to x and subtracts 6px from y in the image
        # The next block on the Y axis adds 12px to x and adds 6px to y in the image
        # The next block up on the Z axis subtracts 12 from y axis in the image

        # Since there are 16x16x128 blocks in a chunk, the image will be 384x1728
        # (height is 128*12 high, plus the size of the horizontal plane: 16*12)
        if not img:
            img = Image.new("RGBA", (384, 1728))

        for x in xrange(15,-1,-1):
            for y in xrange(16):
                imgx = xoff + x*12 + y*12
                imgy = yoff - x*6 + y*6 + 128*12 + 16*12//2
                for z in xrange(128):
                    try:
                        blockid = blocks[x,y,z]
                        t = textures.blockmap[blockid]
                        if not t:
                            continue

                        # Check if this block is occluded
                        if cave and (
                                x == 0 and y != 15 and z != 127
                        ):
                            # If it's on the x face, only render if there's a
                            # transparent block in the y+1 direction OR the z-1
                            # direction
                            if (
                                blocks[x,y+1,z] not in transparent_blocks and
                                blocks[x,y,z+1] not in transparent_blocks
                            ):
                                continue
                        elif cave and (
                                y == 15 and x != 0 and z != 127
                        ):
                            # If it's on the facing y face, only render if there's
                            # a transparent block in the x-1 direction OR the z-1
                            # direction
                            if (
                                blocks[x-1,y,z] not in transparent_blocks and
                                blocks[x,y,z+1] not in transparent_blocks
                            ):
                                continue
                        elif cave and (
                                y == 15 and x == 0
                        ):
                            # If it's on the facing edge, only render if what's
                            # above it is transparent
                            if (
                                blocks[x,y,z+1] not in transparent_blocks
                            ):
                                continue
                        elif (
                                # Normal block or not cave mode, check sides for
                                # transparentcy or render unconditionally if it's
                                # on a shown face
                                x != 0 and y != 15 and z != 127 and
                                blocks[x-1,y,z] not in transparent_blocks and
                                blocks[x,y+1,z] not in transparent_blocks and
                                blocks[x,y,z+1] not in transparent_blocks
                        ):
                            # Don't render if all sides aren't transparent and
                            # we're not on the edge
                            continue

                        # Draw the actual block on the image. For cave images,
                        # tint the block with a color proportional to its depth
                        if cave:
                            img.paste(Image.blend(t[0],depth_colors[z],0.3), (imgx, imgy), t[1])
                        else:
                            img.paste(t[0], (imgx, imgy), t[1])

                        # Draw edge lines
                        if blockid not in transparent_blocks:
                            draw = ImageDraw.Draw(img)
                            if x != 15 and blocks[x+1,y,z] == 0:
                                draw.line(((imgx+12,imgy), (imgx+24,imgy+6)), fill=(0,0,0), width=1)
                            if y != 0 and blocks[x,y-1,z] == 0:
                                draw.line(((imgx,imgy+6), (imgx+12,imgy)), fill=(0,0,0), width=1)


                    finally:
                        # Do this no mater how the above block exits
                        imgy -= 12

        return img


# Render 128 different color images for color coded depth blending in cave mode
def generate_depthcolors():
    depth_colors = []
    r = 255
    g = 0
    b = 0
    for z in range(128):
        img = Image.new("RGB", (24,24), (r,g,b))
        depth_colors.append(img)
        if z < 32:
            g += 7
        elif z < 64:
            r -= 7
        elif z < 96:
            b += 7
        else:
            g -= 7

    return depth_colors
depth_colors = generate_depthcolors()