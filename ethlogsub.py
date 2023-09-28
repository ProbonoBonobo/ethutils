from web3 import Web3, eth
import eth_abi

from websockets import connect

import json
import time
import datetime
import asyncio
import threading


class EthLogSubscriber():
    """A get-and-stay-current Ethereum log listener"""
    
    def __init__(self, wss_provider_url, address, event_abi, callback_function, fromBlock='latest', fromDate=None, dateTolerance=60):
        """
        This code defines an initializer method for a class. The method takes several parameters including a
        WebSocket endpoint URL, a contract address, an event ABI, a callback function, and optional parameters for
        filtering events. The method initializes the instance variables of the class with the provided values. If a
        fromDate is provided, it searches for the closest block within a specified tolerance and sets the fromBlock
        to that block number.
        Parameters ----------
        name : str
            wss_provider_url WSS endpoint e.g. wss://sepolia.infura.io/ws/v3/TOKEN

        address : str
            Contract address to watch e.g. '0xcbc671fb042ee2844a2e014477406369ab99efd7'
        event_abi : dict
            A single name + array of inputs ABI JSON-spec that identifies the
            specific event to filter on.

        callback_function : function
            Called for both historic lookups and every time a new event comes
            in. The callback signature is  void f(log: dict, args: dict)
            The log dict contains txhash and such.  args is decoded from the
            inbound event data based on the ABI.

        fromBlock : int, optional
            Filter from this block number (inclusive) forward.  If omitted,
            no historical lookup is performed and only new events as of the
            moment of subscription will be picked up.

        fromDate : datetime, optional
            Try to find the closest block within dateTolerance seconds AFTER
            the given UTC datetime; be careful about UTC vs TZ aware datetimes.
            If such a block cannot be found, an exception is raised.
            This arg will override fromBlock.  A binary search algorithm is
            used so with Ethereum mainnet at 17443816 blocks, log2() gives
            a max 24 hits to blockchain.  Note there is no native ethereum
            'eth_getBlockByTimestamp' function.

        dateTolerance : int, optional
            Only applicable when fromDate is used.  Default is 60 seconds.
            The closest block after the fromDate time within dateTolerance is
            a candidate.
        
        
        Note that you must call start() on the object to actually get it going.
        """
        self.wss_provider_url = wss_provider_url
        self.address = address
        self.abi = event_abi
        self.callback_function = callback_function

        # 'latest' is a web3 enum keyword.  fromBlock can also be an int
        self.fromBlock = fromBlock

        self.captured = []
        self.seen = []
        self.capaction = -1  # -1 means first time, 0 means off, 1 means on

        self.bgthread = None

        if fromDate is not None:
            ts = int(fromDate.timestamp())
            blk = self._find_block(ts, tolerance=dateTolerance)

            if blk is None:
                raise ValueError("cannot find block within %d seconds of %s" % (dateTolerance,fromDate))
            else:
                self.fromBlock = blk['number'] # override arg fromBlock:

                
    
    def _listener(self):
        """
        This method sets up an event listener for a specific Ethereum event.
        """
        #  Set up these two because they are used over and over again:
        types = [i['type'] for i in self.abi['inputs']]
        names = [i['name'] for i in self.abi['inputs']]

        # First we extracting the event types and names from the provided ABI (Application Binary Interface). It
        # then calculates the keccak hash of the event signature using the event name and types.

        # Ha!  Yeah, we don't call the arg type normalizers (e.g. you cannot
        # use the alias 'uint'; you must use 'uint256').
        #   from web3._utils.abi import abi_to_signature 
        # looked promising but doesn't really do anything more than this and
        # you still have to keccak it, so let's do it here:
        ev_sig_hash = Web3.keccak(text="%s(%s)" % (self.abi['name'], ",".join(types))).hex()

        def _mk_args(log):
            """
            Decodes the event log data and returns a dictionary mapping the event parameter names
            to their corresponding values.
            """
            data = log['data']
            bb = bytes.fromhex(data[2:]) # Remember to jump over 0x...
            values = eth_abi.decode(types, bb)  # The Juice
            return dict(zip(names, values))

        def _drain_captured():
            """
            Iterates over the captured event logs and calls the callback function with the event log and its
            decoded arguments. It clears the captured logs after processing them.
            :return:
            """
            if len(self.captured) > 0:
                for item in self.captured:
                    if item['blockNumber'] not in self.seen:
                        args = _mk_args(item)
                        self.callback_function(item, args)
                self.captured = []
            
        async def get_event():
            """
            Establishes a WebSocket connection to an Ethereum provider and subscribes to logs related to the
            specified event signature. It then listens for incoming messages and processes the logs by calling
            the appropriate functions based on the capaction value.
            """
            async with connect(self.wss_provider_url) as wsock:

                cmd = {"id": 1, "method": "eth_subscribe",
                       "params": [
                           "logs", {"address": self.address,
                                    "topics": [ev_sig_hash] }
                       ]
                }

                await wsock.send(json.dumps(cmd))
                subscription_response = await wsock.recv()

                while True:
                    if self.capaction == 0:
                        break # end the wait_for loop
                    
                    try:
                        message = await asyncio.wait_for(wsock.recv(), timeout=10)
                        message = json.loads(message) # reform as dict
                        log = message['params']['result'] # TBD pluggable?

                        # contract.filter.get_all_entries() produces nice
                        # clean logs.  The low level JSON-RPC method produces
                        # logs in raw form.  In particular, blockNumber
                        # is not a long like in the w3.eth.contract facilitated
                        # historical call.  We have to post-process..
                        log['blockNumber'] = int(log['blockNumber'], 16)

                        if self.capaction == -1:
                            # Historical running; capture this log:
                            self.captured.append(log)
                        else:
                            _drain_captured()

                            args = _mk_args(log)
                            self.callback_function(log, args)
                            
                    except Exception as e:
                        # There is a condition where capaction is not -1
                        # but no new message has come in to trigger check
                        # for drain.  We must ALSO check when a timeout hits:
                        if self.capaction != -1:
                            _drain_captured()                        
                        #print("exc:",e)
                        #pass

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        loop.run_until_complete(get_event())


    def _find_block(self, timestamp, tolerance=60):
        """
        Searches for a block in a blockchain with a timestamp close to a given timestamp. It uses binary search to
        efficiently find the block. The function takes in a timestamp and an optional tolerance parameter. It
        initializes a Web3 object and retrieves the highest block number from the blockchain. It then iteratively
        searches for a block with a timestamp close to the given timestamp within the specified tolerance. Once a
        candidate block is found, it returns it. Finally, it cleans up the Web3 provider and returns the candidate
        block.

        """
        w3 = Web3(Web3.WebsocketProvider(self.wss_provider_url, websocket_timeout=60))
        highest_block = w3.eth.block_number
        block = w3.eth.get_block(highest_block)        
        lowest_block = 0

        candidate = None

        while lowest_block <= highest_block:
            mid_block = (lowest_block + highest_block) // 2  # // is int divide

            block = w3.eth.get_block(mid_block)

            closeness = block.timestamp - timestamp

            if closeness >= 0 and closeness <= tolerance:
                candidate = block

            if closeness == 0: 
                break  # No point in going any further...

            if block.timestamp < timestamp:
                lowest_block = mid_block + 1
            else:
                highest_block = mid_block - 1
                
        w3.provider = None          #w3.close()
        return candidate


    def _do_historic(self):
        """
        Initializes a Web3 object, creates a filter for events in the contract, retrieves the event logs,
        uninstalls the filter, and then processes the logs by calling a callback function and appending some
        data to a list.
        """

        def _managed_append(array, max_len, new_item):
            if max_len is not None and len(array) == max_len:
                array.pop(0)
            array.append(new_item)

        w3 = Web3(Web3.WebsocketProvider(self.wss_provider_url, websocket_timeout=60))
        chk_addr   = w3.to_checksum_address(self.address)
        full_abi = [ self.abi ] # must give array of function decls to w3.eth.contract():
        contract = w3.eth.contract(address=chk_addr, abi=full_abi)

        filter = contract.events[self.abi['name']].create_filter(fromBlock=self.fromBlock)
        logs = filter.get_all_entries() # this is actual data fetch 
        rc = w3.eth.uninstall_filter(filter.filter_id)

        w3.provider = None          #w3.close()

        if len(logs) > 0:
            # Unlike the listener, we do not need to call the decoder
            # because the contract/filter APIs do that for us:
            for log in logs:
                self.callback_function(log, log['args'])
                self.seen.append(log['blockNumber'])
                _managed_append(self.seen, self.max_seen, log['blockNumber'])

                
    def start(self):
        """
        Starts a background thread that runs a listener function _listener. If capaction is equal to 1, the method
        returns early to avoid multiple starts. If capaction is equal to -1, the method waits for 0.2 seconds before
        continuing. Then, if fromBlock is not equal to 'latest', the method calls another function _do_historic.
        Finally, capaction is set to 1.
        """
        
        if self.capaction == 1:
            return  # defend against multiple starts...
        
        self.bgthread = threading.Thread(target=self._listener, args=())
        self.bgthread.start()

        if self.capaction == -1:
            # First time in!  Give thread above a chance to
            # get connected; there are some race conditions in web3.py...
            time.sleep(0.2)

        if self.fromBlock != 'latest':            
            self._do_historic()

        self.capaction = 1

        
    def stop(self):
        self.capaction = 0
        self.bgthread.join()





def main():
    """
    The main function is the entry point of the program. It performs the following tasks:

    - Checks if the environment variable 'WSS_PROVIDER' is set. If not, it prints an error message and exits.
    - Retrieves the value of the 'WSS_PROVIDER' environment variable.
    - Defines an event ABI dictionary with the type, name, and inputs.
    - Initializes a 'Dummy' class instance.
    - Starts the event listener in a separate thread.
    - Prints the number of historic events seen by the event listener.
    - Performs other work for 30 seconds.
    - Stops the event listener.

    Note: The 'Dummy' class has a callback function that is executed when a new event is received.
    """
    import os

    envvar = 'WSS_PROVIDER'
    if envvar not in os.environ:
        print("Please set envvar %s to the Sepolia WSS provider" % envvar)
        return
    
    wss_provider_url = os.environ[envvar]
    address = '0xcbc671fb042ee2844a2e014477406369ab99efd7'
    event_abi = {
        "type": "event",
        "name": "Cheapo",
        "inputs": [
            {
                "type": "bytes",
                "name": "BSON_PAYLOAD",
                "indexed": False
            }
            ,{
                "type": "bytes32",
                "name": "khash",
                "indexed": False
            }
        ],
        #  False is default BUT there is quasi-bug in web3.py that demands
        #  that we set field 'anonymous'.  Because we supply "name" above,
        #  clearly this function is NOT anonymous...
        "anonymous": False
    }

    fromBlock = 3657458 #0 # 'latest' 

    class Dummy():
        def cbk(self, log, args):
            print("blknum %d; %s" % (log['blockNumber'],self.value))
            print("log", log)
            print("args", args)        
        
        def __init__(self):
            self.value = "anything"

            # Note hours+4 because I am ET.
            dt = datetime.datetime(2023, 6, 9, 16 + 4, 47, 7, 0, tzinfo=datetime.timezone.utc)
            self.ctx = EthLogSubscriber(wss_provider_url, address, event_abi, self.cbk, fromDate=dt)

        def go(self):            
            self.ctx.start()
            print("historic seen:", len(self.ctx.seen), self.ctx.seen)
            print("subthread running.  Do other work here.  cbk will be hit async")
            # If you do nothing from here, after go() finishes the main()
            # will not exit because of the subthread.  Pretend to do some work
            # here...
            time.sleep(30)
            
            print("initiating stop(); will block waiting for loop to break out...")    
            self.ctx.stop()
            
    dd = Dummy()
    dd.go()
    
if __name__ == '__main__':
    main()
