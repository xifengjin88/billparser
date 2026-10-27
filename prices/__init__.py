"""The price catalog: the providers' public list prices (AWS first: the Price List bulk files, with on demand, Reserved
Instance and Savings Plans rates), stored with their history in billparse's database and looked up in bulk by rate
code, SKU, usage type or attributes as of a date (prices.load, prices.store). Task 06 fills a line's list price
from it."""
