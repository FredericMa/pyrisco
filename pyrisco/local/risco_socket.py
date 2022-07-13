import asyncio
from .risco_crypt import RiscoCrypt, ESCAPED_END, END
from pyrisco.risco import UnauthorizedError, CannotConnectError, OperationError

MIN_CMD_ID = 1
MAX_CMD_ID = 49

class RiscoSocket:
  def __init__(self, options):
    self._panel_id = options['panel_id']
    self._encoding = options['encoding']
    self._host = options['host']
    self._port = options['port']
    self._code_length = options['code_length']
    self._code = options['code']
    self._reader = None
    self._writer = None
    self._listen_task = None
    self._keep_alive_task = None
    self._max_concurrency = options['concurrency']
    self._semaphore = None

  async def connect(self):
    self._semaphore = asyncio.Semaphore(self._max_concurrency)
    self._cmd_id = 0
    self._futures = [None for i in range(MIN_CMD_ID,MIN_CMD_ID + MAX_CMD_ID)]
    self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
    self._crypt = RiscoCrypt(self._panel_id, self._encoding)
    self._listen_task = asyncio.create_task(self._listen())

    try:
      command = f'RMT={self._code:0{self._code_length}d}'
      if not await self.send_ack_command(command):
        raise CannotConnectError

      if not await self.send_ack_command('LCL'):
        raise CannotConnectError

      self._crypt.encrypted_panel = True
      if not await self.send_ack_command('LCL'):
        raise UnauthorizedError
      self._keep_alive_task = asyncio.create_task(self._keep_alive())
    except:
      await self._close()
      raise

  async def disconnect(self):
    if self._writer:
      try:
        await self.send_ack_command('DCN')
      finally:
        await self._close()

  async def _listen(self):
    while True:
      cmd_id, command, crc = await self._read_command()
      if cmd_id <= MAX_CMD_ID:
        future = self._futures[cmd_id-1]
        self._futures[cmd_id-1] = None
        if not crc:
          future.set_exception(OperationError(f'cmd_id: {cmd_id}, Wrong CRC'))
        elif command[0] in ['N', 'B']:
          future.set_exception(OperationError(f'cmd_id: {cmd_id}, Risco error: {command}'))
        else:
          future.set_result(command)
      else:
        self._handle_incoming(cmd_id, command, crc)


  async def _keep_alive(self):
    while True:
      await self.send_result_command("CLOCK")
      await asyncio.sleep(5)

  async def send_ack_command(self, command):
    command = await self.send_command(command)
    return command == 'ACK'

  async def send_result_command(self, command):
    command = await self.send_command(command)
    return command.split("=")[1]

  async def send_command(self, command, force_encryption=False):
    async with self._semaphore:
      self._advance_cmd_id()
      self._write_command(self._cmd_id, command, force_encryption)
      future = asyncio.Future()
      self._futures[self._cmd_id-1] = future
      return await future

  def _handle_incoming(self, cmd_id, command, crc):
    print(f'ID: {cmd_id}, command: {command}, crc: {crc}')
    self._write_command(cmd_id, 'ACK')

  async def _read_command(self):
    buffer = await self._reader.readuntil(END)
    while buffer.endswith(ESCAPED_END):
      buffer += await self._reader.readuntil(END)
    return self._crypt.decode(buffer)

  def _write_command(self, cmd_id, command, force_encryption=False):
    buffer = self._crypt.encode(cmd_id, command, force_encryption)
    self._writer.write(buffer)

  async def _close(self):
    if self._keep_alive_task:
      self._keep_alive_task.cancel()
      self._keep_alive_task = None

    if self._listen_task:
      self._listen_task.cancel()
      self._listen_task = None

    self._writer.close()
    await self._writer.wait_closed()
    self._writer = None
    self._reader = None
    self._semaphore = None

  def _advance_cmd_id(self):
    self._cmd_id += 1
    if self._cmd_id > MAX_CMD_ID:
      self._cmd_id = MIN_CMD_ID
